"""Render timeline media, subtitles, and animated 2D characters to MP4.

This path remains separate from VO-SE synthesis and the native compositor.
The first offline character renderer intentionally uses the built-in 2D
placeholder; external VRM/FBX models still require the future 3D renderer.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from media_ffmpeg import (
    MediaToolError,
    find_ffmpeg_executable,
    probe_video_dimensions,
)
from timeline_model import TimelineClip, TimelineModel
from subtitle_overlay import render_subtitle_png
from character_motion import CharacterMotionError, load_motion_file, sample_motion
from character_renderer import render_character_frame
from vrm_mocap import motion_state_to_vrm_pose
from lipsync import extract_lipsync_frames, mouth_openness_at, vowels_at

BUILTIN_2D_CHARACTER_ID = "__aural_builtin_2d_character__"


class VideoRenderError(RuntimeError):
    """Raised when the timeline cannot be rendered as a video."""


@dataclass(frozen=True)
class VideoRenderResult:
    output_path: Path
    duration_seconds: float
    video_clip_count: int
    width: int
    height: int
    frame_rate: float
    subtitle_clip_count: int = 0
    character_clip_count: int = 0
    motion_clip_count: int = 0


def timeline_motion_clips(model: TimelineModel) -> list[TimelineClip]:
    """Return enabled motion clips on visible, unmuted motion tracks."""
    characters = [
        clip for clip in model.clips
        if clip.kind == "character" and clip.enabled
        and (track := model.get_track(clip.track_id)) is not None
        and not track.muted and track.visible
    ]
    selected: list[TimelineClip] = []
    for track in model.tracks:
        if track.kind != "motion" or track.muted or not track.visible:
            continue
        for clip in model.clips_for_track(track.id):
            if not clip.enabled:
                continue
            if clip.asset_id is None:
                raise VideoRenderError(
                    f"モーションクリップ「{clip.name}」にモーションJSON素材がありません"
                )
            if not any(
                clip.start < character.end and clip.end > character.start
                for character in characters
            ):
                raise VideoRenderError(
                    f"モーションクリップ「{clip.name}」に重なるキャラクタークリップがありません"
                )
            selected.append(clip)
    return selected


def timeline_video_clips(
    model: TimelineModel,
    subtitle_texts: Mapping[str, str] | None = None,
    *,
    allow_vrm: bool = False,
) -> list[TimelineClip]:
    """Return enabled visible video, image, subtitle and built-in character clips."""
    selected: list[TimelineClip] = []
    unsupported: list[str] = []
    for track in model.tracks:
        if track.muted or not track.visible:
            continue
        if track.kind == "motion":
            continue
        if track.kind not in {"video", "overlay", "other"}:
            continue
        for clip in model.clips_for_track(track.id):
            if not clip.enabled:
                continue
            if clip.kind == "motion":
                unsupported.append(f"motion: {clip.name}")
            elif clip.kind == "character":
                if clip.asset_id is not None and not allow_vrm:
                    unsupported.append(
                        f"3D character asset: {clip.name} "
                        "(VRMのオフライン描画にはQt WebEngineが必要です)"
                    )
                elif clip.asset_id is None and clip.script_line_id != BUILTIN_2D_CHARACTER_ID:
                    unsupported.append(
                        f"character: {clip.name} (組み込み2Dキャラクターとして追加してください)"
                    )
                else:
                    selected.append(clip)
            elif clip.kind == "text":
                text = (
                    subtitle_texts.get(clip.script_line_id or "")
                    if subtitle_texts is not None else None
                )
                text = text if text is not None else clip.name
                if not text.strip():
                    raise VideoRenderError(
                        f"字幕クリップ「{clip.name or clip.id}」に表示テキストがありません"
                    )
                selected.append(clip)
            elif clip.kind in {"video", "image"}:
                if clip.asset_id is None:
                    raise VideoRenderError(
                        f"映像クリップ「{clip.name}」に素材参照がありません"
                    )
                selected.append(clip)

    if unsupported:
        examples = "、".join(unsupported[:5])
        extra = "" if len(unsupported) <= 5 else f" ほか {len(unsupported) - 5} 件"
        raise VideoRenderError(
            "現在のMP4書き出しで描画できないクリップがあります: "
            f"{examples}{extra}。対応する2Dキャラクター／モーション素材を使ってください。"
        )
    return selected


def _seconds(value: float, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise VideoRenderError(f"{label} must be a number") from exc
    if not math.isfinite(result) or result < 0.0:
        raise VideoRenderError(f"{label} must be finite and non-negative")
    return result


def _filter_number(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def _fade_gain(clip: TimelineClip, timeline_time: float) -> float:
    gain = clip.volume
    local = timeline_time - clip.start
    if clip.fade_in > 0.0 and local < clip.fade_in:
        gain *= max(0.0, local / clip.fade_in)
    remaining = clip.end - timeline_time
    if clip.fade_out > 0.0 and remaining < clip.fade_out:
        gain *= max(0.0, remaining / clip.fade_out)
    return max(0.0, min(1.0, gain))


def _load_voice_lipsync(
    voice_clips: Iterable[TimelineClip],
    asset_resolver: Callable[[TimelineClip], str | Path],
    frame_rate: float,
) -> list[tuple[TimelineClip, list]]:
    results = []
    for clip in voice_clips:
        if clip.kind != "voice" or not clip.enabled:
            continue
        try:
            source = Path(asset_resolver(clip)).expanduser().resolve()
            if not source.is_file():
                continue
            # Generated VO-SE WAVs are PCM16. Other audio formats continue to
            # render normally; motion keyframes remain available for those clips.
            with wave.open(str(source), "rb") as wav:
                if wav.getsampwidth() != 2:
                    continue
            frames = extract_lipsync_frames(str(source), fps=frame_rate)
            results.append((clip, frames))
        except (OSError, ValueError, wave.Error):
            continue
    return results


def _character_state_at(
    timeline_time: float,
    motion_entries: list[tuple[TimelineClip, list[dict]]],
    voice_entries: list[tuple[TimelineClip, list]],
) -> dict:
    state = {
        "mouth_openness": 0.0,
        "head": {"x": 0.0, "y": 0.0, "tilt_deg": 0.0},
        "blend_shapes": {},
        "vowels": {},
        "bones": {},
    }
    active_motion = [
        (clip, frames) for clip, frames in motion_entries
        if clip.start <= timeline_time < clip.end
    ]
    if active_motion:
        if len(active_motion) > 1:
            raise VideoRenderError(
                "同じ時間帯に複数のモーションクリップが重なっています。"
                "2Dキャラクターごとに1つのモーションクリップを配置してください。"
            )
        clip, frames = active_motion[0]
        state = sample_motion(frames, clip.source_in + timeline_time - clip.start)

    mouth = float(state["mouth_openness"])
    vowels = dict(state["vowels"])
    for clip, frames in voice_entries:
        if not (clip.start <= timeline_time < clip.end):
            continue
        local_time = clip.source_in + timeline_time - clip.start
        gain = _fade_gain(clip, timeline_time)
        mouth = max(mouth, mouth_openness_at(frames, local_time) * gain)
        for vowel, weight in vowels_at(frames, local_time).items():
            vowels[vowel] = max(vowels.get(vowel, 0.0), weight * gain)
    state["mouth_openness"] = max(0.0, min(1.0, mouth))
    state["vowels"] = vowels
    return state


def _render_character_sequence(
    clip: TimelineClip,
    sequence_dir: Path,
    frame_rate: float,
    motion_entries: list[tuple[TimelineClip, list[dict]]],
    voice_entries: list[tuple[TimelineClip, list]],
) -> Path:
    sequence_dir.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, int(math.ceil(clip.duration * frame_rate - 1e-9)))
    for frame_index in range(frame_count):
        local_time = min(frame_index / frame_rate, max(0.0, clip.duration - 1e-9))
        timeline_time = clip.start + local_time
        state = _character_state_at(timeline_time, motion_entries, voice_entries)
        head = state["head"]
        frame = render_character_frame(
            mouth_openness=state["mouth_openness"],
            time_sec=local_time,
            head_offset_x=head.get("x", 0.0),
            head_offset_y=head.get("y", 0.0),
            head_tilt_deg=head.get("tilt_deg", 0.0),
            blend_shapes=state["blend_shapes"],
            vowels=state["vowels"],
            bones=state["bones"],
        )
        frame.save(sequence_dir / f"frame_{frame_index:06d}.png", format="PNG", optimize=True)
    return sequence_dir / "frame_%06d.png"


def _render_vrm_sequence(
    clip: TimelineClip,
    model_path: Path,
    sequence_dir: Path,
    frame_rate: float,
    motion_entries: list[tuple[TimelineClip, list[dict]]],
    voice_entries: list[tuple[TimelineClip, list]],
    frame_renderer: Callable[[str | Path, dict, float], bytes],
) -> Path:
    """Render a VRM clip as transparent PNG frames using the Qt WebEngine bridge."""
    if model_path.suffix.lower() != ".vrm" or not model_path.is_file():
        raise VideoRenderError(
            f"3Dキャラクター「{clip.name}」の素材はVRM 0.x / 1.0の .vrm が必要です"
        )
    sequence_dir.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, int(math.ceil(clip.duration * frame_rate - 1e-9)))
    for frame_index in range(frame_count):
        local_time = min(frame_index / frame_rate, max(0.0, clip.duration - 1e-9))
        timeline_time = clip.start + local_time
        state = _character_state_at(timeline_time, motion_entries, voice_entries)
        pose = motion_state_to_vrm_pose(state)
        try:
            image = frame_renderer(model_path, pose, frame_rate)
        except Exception as exc:
            raise VideoRenderError(
                f"VRMキャラクター「{clip.name}」のフレーム {frame_index} を描画できません: {exc}"
            ) from exc
        if not isinstance(image, bytes) or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise VideoRenderError(
                f"VRMキャラクター「{clip.name}」のフレーム {frame_index} が有効なPNGではありません"
            )
        (sequence_dir / f"frame_{frame_index:06d}.png").write_bytes(image)
    return sequence_dir / "frame_%06d.png"


def render_timeline_video(
    model: TimelineModel,
    asset_resolver: Callable[[TimelineClip], str | Path],
    output_path: str | Path,
    *,
    duration_seconds: float | None = None,
    ffmpeg_path: str | Path | None = None,
    still_image_canvas: tuple[int, int] = (1920, 1080),
    subtitle_texts: Mapping[str, str] | None = None,
    motion_clips: Iterable[TimelineClip] | None = None,
    voice_clips: Iterable[TimelineClip] = (),
    vrm_frame_renderer: Callable[[str | Path, dict, float], bytes] | None = None,
) -> VideoRenderResult:
    """Composite enabled visual clips and animated 2D characters into an MP4.

    Character clips use the built-in transparent 2D placeholder. Keyframed
    motion is loaded from aural-motion JSON assets and voice clips provide
    automatic mouth movement when they are PCM16 WAV files.
    """
    clips = timeline_video_clips(
        model, subtitle_texts, allow_vrm=vrm_frame_renderer is not None
    )
    selected_motion = list(motion_clips) if motion_clips is not None else timeline_motion_clips(model)
    selected_voice = list(voice_clips)

    if not clips:
        raise VideoRenderError("有効で表示中の動画・画像・字幕・キャラクタークリップがありません")

    frame_rate = _seconds(model.frame_rate, "timeline frame rate")
    if not 1.0 <= frame_rate <= 240.0:
        raise VideoRenderError("タイムラインのフレームレートは1〜240fpsにしてください")

    natural_duration = max(
        [clip.end for clip in clips]
        + [clip.end for clip in selected_voice if clip.enabled]
    )
    duration = natural_duration if duration_seconds is None else _seconds(
        duration_seconds, "render duration"
    )
    if duration <= 0.0 or duration + 1e-6 < natural_duration:
        raise VideoRenderError("書き出し時間が映像・音声クリップの終端より短くなっています")

    resolved: list[tuple[TimelineClip, Path | None]] = []
    protected_sources: set[Path] = set()
    for clip in clips:
        if clip.kind == "text" or (
            clip.kind == "character" and clip.asset_id is None
        ):
            resolved.append((clip, None))
            continue
        try:
            source = Path(asset_resolver(clip)).expanduser().resolve()
        except Exception as exc:
            raise VideoRenderError(
                f"映像クリップ「{clip.name}」の素材パスを解決できません: {exc}"
            ) from exc
        if not source.is_file():
            raise VideoRenderError(
                f"映像クリップ「{clip.name}」の素材ファイルが見つかりません: {source}"
            )
        if clip.kind == "character" and source.suffix.lower() != ".vrm":
            raise VideoRenderError(
                f"3Dキャラクター「{clip.name}」はVRM 0.x / 1.0の .vrm のみ対応しています"
            )
        protected_sources.add(source)
        resolved.append((clip, source))

    motion_entries: list[tuple[TimelineClip, list[dict]]] = []
    for clip in selected_motion:
        try:
            source = Path(asset_resolver(clip)).expanduser().resolve()
            frames = load_motion_file(source)
        except (OSError, CharacterMotionError, ValueError) as exc:
            raise VideoRenderError(
                f"モーションクリップ「{clip.name}」を読み込めません: {exc}"
            ) from exc
        protected_sources.add(source)
        motion_entries.append((clip, frames))

    voice_entries = _load_voice_lipsync(selected_voice, asset_resolver, frame_rate)
    for clip in selected_voice:
        if clip.kind == "voice" and clip.enabled:
            try:
                protected_sources.add(Path(asset_resolver(clip)).expanduser().resolve())
            except (OSError, ValueError):
                pass

    destination = Path(output_path).expanduser()
    if destination.suffix.lower() != ".mp4":
        raise VideoRenderError("タイムライン映像の出力先は .mp4 を指定してください")
    try:
        destination = destination.resolve()
        if destination in protected_sources:
            raise VideoRenderError("入力素材を上書きすることはできません")
    except OSError as exc:
        raise VideoRenderError(f"出力先を確認できません: {exc}") from exc

    first_video = next(
        ((clip, path) for clip, path in resolved if clip.kind == "video" and path is not None),
        None,
    )
    try:
        if first_video is not None:
            width, height = probe_video_dimensions(first_video[1], ffmpeg_path=ffmpeg_path)
        else:
            width, height = still_image_canvas
            width, height = int(width), int(height)
    except MediaToolError as exc:
        raise VideoRenderError(str(exc)) from exc

    width -= width % 2
    height -= height % 2
    if width < 2 or height < 2:
        raise VideoRenderError(f"出力解像度が不正です: {width}x{height}")

    executable = find_ffmpeg_executable(ffmpeg_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VideoRenderError(f"出力フォルダーを作成できません: {exc}") from exc

    work_dir = Path(tempfile.mkdtemp(prefix="aural-timeline-render-"))
    subtitle_dir = work_dir / "subtitles"
    character_dir = work_dir / "characters"
    subtitle_dir.mkdir()
    character_dir.mkdir()
    temporary: Path | None = None
    try:
        command = [
            executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        ]
        filters = [
            f"color=c=black:s={width}x{height}:r={_filter_number(frame_rate)}:"
            f"d={_filter_number(duration)},format=rgba[base0]"
        ]
        for index, (clip, source) in enumerate(resolved):
            if clip.kind == "character":
                overlapping_motion = [
                    (motion, frames)
                    for motion, frames in motion_entries
                    if motion.start < clip.end and motion.end > clip.start
                ]
                if source is not None:
                    if vrm_frame_renderer is None:
                        raise VideoRenderError(
                            f"VRMキャラクター「{clip.name}」のフレームレンダラーがありません"
                        )
                    pattern = _render_vrm_sequence(
                        clip,
                        source,
                        character_dir / f"character_{index}",
                        frame_rate,
                        overlapping_motion,
                        voice_entries,
                        vrm_frame_renderer,
                    )
                else:
                    pattern = _render_character_sequence(
                        clip,
                        character_dir / f"character_{index}",
                        frame_rate,
                        overlapping_motion,
                        voice_entries,
                    )
                command.extend([
                    "-framerate", _filter_number(frame_rate),
                    "-start_number", "0",
                    "-i", str(pattern),
                ])
            elif clip.kind in {"image", "text"}:
                if clip.kind == "image":
                    if source is None:
                        raise VideoRenderError(f"静止画クリップ「{clip.name}」の素材がありません")
                    image_source = source
                else:
                    subtitle_text = (
                        subtitle_texts.get(clip.script_line_id or "")
                        if subtitle_texts is not None else None
                    )
                    subtitle_text = subtitle_text if subtitle_text is not None else clip.name
                    if not subtitle_text.strip():
                        raise VideoRenderError(
                            f"字幕クリップ「{clip.name}」に表示テキストがありません"
                        )
                    image_source = render_subtitle_png(
                        subtitle_text,
                        width,
                        height,
                        subtitle_dir / f"subtitle_{index}.png",
                    )
                command.extend([
                    "-loop", "1",
                    "-framerate", _filter_number(frame_rate),
                    "-i", str(image_source),
                ])
            else:
                if source is None:
                    raise VideoRenderError(f"動画クリップ「{clip.name}」の素材がありません")
                command.extend(["-i", str(source)])

        previous_label = "base0"
        for index, (clip, _source) in enumerate(resolved):
            source_label = f"clip{index}"
            if clip.kind == "character":
                filters.append(
                    f"[{index}:v:0]trim=duration={_filter_number(clip.duration)},"
                    f"setpts=PTS-STARTPTS+{_filter_number(clip.start)}/TB,"
                    f"scale=-1:{max(2, int(height * 0.4) // 2 * 2)},"
                    f"format=rgba[{source_label}]"
                )
                margin = max(8, int(height * 0.03))
                next_label = f"base{index + 1}"
                enable = (
                    f"gte(t,{_filter_number(clip.start)})*"
                    f"lt(t,{_filter_number(clip.end)})"
                )
                filters.append(
                    f"[{previous_label}][{source_label}]"
                    f"overlay=x={margin}:y=H-h-{margin}:eof_action=pass:"
                    f"repeatlast=0:shortest=0:enable='{enable}'[{next_label}]"
                )
                previous_label = next_label
                continue

            if clip.kind == "video":
                source_trim = (
                    f"trim=start={_filter_number(clip.source_in)}:"
                    f"duration={_filter_number(clip.duration)}"
                )
            else:
                source_trim = f"trim=duration={_filter_number(clip.duration)}"
            filters.append(
                f"[{index}:v:0]{source_trim},"
                f"setpts=PTS-STARTPTS+{_filter_number(clip.start)}/TB,"
                f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease:"
                f"flags=lanczos,setsar=1,fps={_filter_number(frame_rate)},"
                f"format=rgba[{source_label}]"
            )
            next_label = f"base{index + 1}"
            enable = (
                f"gte(t,{_filter_number(clip.start)})*"
                f"lt(t,{_filter_number(clip.end)})"
            )
            filters.append(
                f"[{previous_label}][{source_label}]"
                f"overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass:"
                f"repeatlast=0:shortest=0:enable='{enable}'[{next_label}]"
            )
            previous_label = next_label

        filters.append(f"[{previous_label}]format=yuv420p[vout]")
        filter_graph = ";".join(filters)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".tmp.mp4",
            dir=str(destination.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)
        command.extend([
            "-filter_complex", filter_graph,
            "-map", "[vout]",
            "-an",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-t", _filter_number(duration),
            str(temporary),
        ])
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise VideoRenderError(f"FFmpegを起動できませんでした: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            raise VideoRenderError(
                "タイムライン映像を書き出せませんでした。入力形式やFFmpegの"
                "libx264/overlay対応を確認してください。"
                + (f"\n{detail}" if detail else "")
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise VideoRenderError("FFmpegのMP4出力が作成されませんでした")
        os.replace(temporary, destination)
    except VideoRenderError:
        raise
    except OSError as exc:
        raise VideoRenderError(f"MP4出力を保存できませんでした: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)

    return VideoRenderResult(
        output_path=destination,
        duration_seconds=duration,
        video_clip_count=sum(1 for clip, _ in resolved if clip.kind in {"video", "image"}),
        width=width,
        height=height,
        frame_rate=frame_rate,
        subtitle_clip_count=sum(1 for clip, _ in resolved if clip.kind == "text"),
        character_clip_count=sum(1 for clip, _ in resolved if clip.kind == "character"),
        motion_clip_count=len(motion_entries),
    )

"""Render video and still-image clips from the aural Studio timeline.

This is a standalone FFmpeg CLI path. It deliberately does not modify the
existing native compositor, VO-SE synthesis, or audio mix implementation.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from media_ffmpeg import (
    MediaToolError,
    find_ffmpeg_executable,
    probe_video_dimensions,
)
from timeline_model import TimelineClip, TimelineModel
from subtitle_overlay import render_subtitle_png


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


def timeline_video_clips(
    model: TimelineModel,
    subtitle_texts: Mapping[str, str] | None = None,
) -> list[TimelineClip]:
    """Return visible, enabled video/image clips in track and timeline order.

    Text clips are rasterized into timed transparent overlays. Live character
    and motion clips are rejected rather than silently omitted.
    """
    selected: list[TimelineClip] = []
    unsupported: list[str] = []
    for track in model.tracks:
        if track.muted or not track.visible:
            continue
        if track.kind == "motion":
            unsupported.extend(
                f"motion: {clip.name}"
                for clip in model.clips_for_track(track.id)
                if clip.enabled
            )
            continue
        if track.kind not in {"video", "overlay", "other"}:
            continue
        for clip in model.clips_for_track(track.id):
            if not clip.enabled:
                continue
            if clip.kind in {"character", "motion"}:
                unsupported.append(f"{clip.kind}: {clip.name}")
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
            "現在のMP4書き出しでは動的キャラクター・モーションクリップを"
            f"映像へ焼き込めません: {examples}{extra}。これらを非表示または削除してから"
            "書き出してください。"
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


def render_timeline_video(
    model: TimelineModel,
    asset_resolver: Callable[[TimelineClip], str | Path],
    output_path: str | Path,
    *,
    duration_seconds: float | None = None,
    ffmpeg_path: str | Path | None = None,
    still_image_canvas: tuple[int, int] = (1920, 1080),
    subtitle_texts: Mapping[str, str] | None = None,
) -> VideoRenderResult:
    """Composite enabled video, still-image and subtitle clips into an MP4.

    Tracks are composited in model order; later tracks appear above earlier
    tracks. Media is centered on a black canvas. Video source trims, subtitle
    timings, timeline offsets and clip durations are applied non-destructively.
    """
    clips = timeline_video_clips(model, subtitle_texts)
    if not clips:
        raise VideoRenderError("有効で表示中の動画・静止画クリップがありません")

    frame_rate = _seconds(model.frame_rate, "timeline frame rate")
    if not 1.0 <= frame_rate <= 240.0:
        raise VideoRenderError("タイムラインのフレームレートは1〜240fpsにしてください")

    natural_duration = max(clip.end for clip in clips)
    duration = natural_duration if duration_seconds is None else _seconds(
        duration_seconds, "render duration"
    )
    if duration <= 0.0 or duration + 1e-6 < natural_duration:
        raise VideoRenderError("書き出し時間が映像クリップの終端より短くなっています")

    resolved: list[tuple[TimelineClip, Path | None]] = []
    for clip in clips:
        if clip.kind == "text":
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
        resolved.append((clip, source))

    destination = Path(output_path).expanduser()
    if destination.suffix.lower() != ".mp4":
        raise VideoRenderError("タイムライン映像の出力先は .mp4 を指定してください")
    try:
        destination = destination.resolve()
        if destination in {path for _clip, path in resolved if path is not None}:
            raise VideoRenderError("元の映像・画像素材を上書きすることはできません")
    except OSError as exc:
        raise VideoRenderError(f"出力先を確認できません: {exc}") from exc

    first_video = next(
        ((clip, path) for clip, path in resolved if clip.kind == "video" and path is not None),
        None,
    )
    try:
        if first_video is not None:
            width, height = probe_video_dimensions(
                first_video[1], ffmpeg_path=ffmpeg_path
            )
        else:
            width, height = still_image_canvas
            width, height = int(width), int(height)
    except MediaToolError as exc:
        raise VideoRenderError(str(exc)) from exc

    # yuv420p requires even dimensions. Preserve the source's approximate size.
    width -= width % 2
    height -= height % 2
    if width < 2 or height < 2:
        raise VideoRenderError(f"出力解像度が不正です: {width}x{height}")

    executable = find_ffmpeg_executable(ffmpeg_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VideoRenderError(f"出力フォルダーを作成できません: {exc}") from exc

    filters = [
        f"color=c=black:s={width}x{height}:r={_filter_number(frame_rate)}:"
        f"d={_filter_number(duration)},format=rgba[base0]"
    ]
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]
    subtitle_temp_dir = tempfile.mkdtemp(prefix="aural-subtitles-")
    try:
        for index, (clip, source) in enumerate(resolved):
            if clip.kind in {"image", "text"}:
                if clip.kind == "image":
                    if source is None:
                        raise VideoRenderError(
                            f"静止画クリップ「{clip.name}」の素材がありません"
                        )
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
                        Path(subtitle_temp_dir) / f"subtitle_{index}.png",
                    )
                command.extend([
                    "-loop", "1",
                    "-framerate", _filter_number(frame_rate),
                    "-i", str(image_source),
                ])
            else:
                if source is None:
                    raise VideoRenderError(
                        f"動画クリップ「{clip.name}」の素材がありません"
                    )
                command.extend(["-i", str(source)])
    except Exception:
        shutil.rmtree(subtitle_temp_dir, ignore_errors=True)
        raise

    previous_label = "base0"
    for index, (clip, _source) in enumerate(resolved):
        source_label = f"clip{index}"
        input_index = index
        if clip.kind == "video":
            source_trim = (
                f"trim=start={_filter_number(clip.source_in)}:"
                f"duration={_filter_number(clip.duration)}"
            )
        else:
            source_trim = f"trim=duration={_filter_number(clip.duration)}"
        filters.append(
            f"[{input_index}:v:0]{source_trim},"
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

    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".tmp.mp4",
            dir=str(destination.parent),
        )
    except OSError as exc:
        shutil.rmtree(subtitle_temp_dir, ignore_errors=True)
        raise VideoRenderError(f"一時MP4ファイルを作成できません: {exc}") from exc
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
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(subtitle_temp_dir, ignore_errors=True)

    return VideoRenderResult(
        output_path=destination,
        duration_seconds=duration,
        video_clip_count=sum(1 for clip, _ in resolved if clip.kind in {"video", "image"}),
        width=width,
        height=height,
        frame_rate=frame_rate,
        subtitle_clip_count=sum(1 for clip, _ in resolved if clip.kind == "text"),
    )

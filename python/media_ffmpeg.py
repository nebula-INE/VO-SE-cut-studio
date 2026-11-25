"""FFmpeg CLI integration for audio decoding and lossless-video muxing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class MediaToolError(RuntimeError):
    """Raised when the FFmpeg executable or a media operation is unavailable."""


@dataclass(frozen=True)
class VideoMuxResult:
    output_path: Path
    video_codec_mode: str
    audio_codec: str
    audio_bitrate: str


def find_ffmpeg_executable(ffmpeg_path: str | Path | None = None) -> str:
    """Find a usable FFmpeg CLI in explicit config, PATH, or bundled locations."""
    candidates: list[str | Path] = []
    if ffmpeg_path is not None:
        candidates.append(ffmpeg_path)
    environment_path = os.environ.get("FFMPEG_BINARY")
    if environment_path:
        candidates.append(environment_path)

    executable_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    located = shutil.which(executable_name)
    if located:
        candidates.append(located)

    module_root = Path(__file__).resolve().parent.parent
    executable_names = [executable_name]
    if os.name == "nt":
        executable_names.append("ffmpeg")
    directories = [
        Path(sys.executable).resolve().parent,
        module_root / "bin",
        module_root / "tools" / "ffmpeg" / "bin",
        module_root / "build",
        module_root / "build" / "Release",
        module_root / "vcpkg" / "installed" / "x64-windows" / "tools" / "ffmpeg",
        module_root / "vcpkg_installed" / "x64-windows" / "tools" / "ffmpeg",
        module_root / "vcpkg_installed" / "x64-windows" / "tools" / "ffmpeg" / "bin",
    ]
    for directory in directories:
        for name in executable_names:
            candidates.append(directory / name)

    for candidate in candidates:
        candidate_text = str(candidate)
        located_candidate = shutil.which(candidate_text)
        if located_candidate:
            return str(Path(located_candidate).resolve())
        path = Path(candidate_text).expanduser()
        if path.is_file():
            return str(path.resolve())

    if ffmpeg_path is not None:
        raise MediaToolError(f"指定されたFFmpeg実行ファイルが見つかりません: {ffmpeg_path}")
    raise MediaToolError(
        "FFmpeg実行ファイルが見つかりません。FFmpegをインストールするか、"
        "環境変数 FFMPEG_BINARY に実行ファイルのパスを設定してください。"
    )


def decode_audio_clip_to_f32le(
    input_path: str | Path,
    output_path: str | Path,
    *,
    source_in: float,
    duration: float,
    sample_rate: int,
    ffmpeg_path: str | Path | None = None,
) -> Path:
    """Decode an audio segment to interleaved stereo float32 PCM at sample_rate."""
    executable = find_ffmpeg_executable(ffmpeg_path)
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise MediaToolError(f"音声素材が見つかりません: {source}")
    if source == destination:
        raise MediaToolError("デコード出力で元の音声素材を上書きできません")

    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(source),
        # Output-side seeking is accurate for audio and honors non-zero source_in.
        "-ss", f"{max(0.0, float(source_in)):.9f}",
        "-t", f"{max(0.0, float(duration)):.9f}",
        "-map", "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac", "2",
        "-ar", str(sample_rate),
        "-c:a", "pcm_f32le",
        "-f", "f32le",
        str(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise MediaToolError(f"FFmpegを起動できませんでした: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise MediaToolError(
            f"音声ファイルをデコードできませんでした: {source}"
            + (f"\n{detail}" if detail else "")
        )
    if not destination.is_file():
        raise MediaToolError("FFmpegのデコード出力が作成されませんでした")
    if destination.stat().st_size == 0:
        raise MediaToolError(f"音声トラックに有効なサンプルがありません: {source}")
    if destination.stat().st_size % 8:
        raise MediaToolError("FFmpegのPCM出力サイズが不正です")
    return destination


def mux_audio_into_video(
    video_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path | None = None,
    audio_bitrate: str = "192k",
) -> VideoMuxResult:
    """Mux a rendered audio mix into an existing video, copying video packets.

    Video is stream-copied to avoid a lossy video re-encode. Audio is encoded as
    AAC for broad MP4 compatibility. The output is staged next to its target
    and only replaces it after FFmpeg succeeds.
    """
    executable = find_ffmpeg_executable(ffmpeg_path)
    video = Path(video_path).expanduser().resolve()
    audio = Path(audio_path).expanduser().resolve()
    destination = Path(output_path).expanduser()
    if not video.is_file():
        raise MediaToolError(f"統合する映像ファイルが見つかりません: {video}")
    if not audio.is_file():
        raise MediaToolError(f"統合する音声ファイルが見つかりません: {audio}")
    if destination.suffix.lower() != ".mp4":
        raise MediaToolError("動画と音声の統合出力は .mp4 を指定してください")
    destination = destination.resolve()
    if destination in {video, audio}:
        raise MediaToolError("入力映像・音声を上書きすることはできません")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaToolError(f"出力フォルダーを作成できません: {exc}") from exc

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=".tmp.mp4",
        dir=str(destination.parent),
    )
    os.close(fd)
    temporary = Path(temporary_name)
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-map_metadata", "0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-shortest",
        "-movflags", "+faststart",
        str(temporary),
    ]
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
            raise MediaToolError(f"FFmpegを起動できませんでした: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            raise MediaToolError(
                "映像と音声をMP4へ統合できませんでした。映像コーデックがMP4で"
                "ストリームコピーできない場合は、H.264形式の映像を指定してください。"
                + (f"\n{detail}" if detail else "")
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise MediaToolError("FFmpegのMP4出力が作成されませんでした")
        os.replace(temporary, destination)
    except MediaToolError:
        raise
    except OSError as exc:
        raise MediaToolError(f"MP4出力を保存できませんでした: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    return VideoMuxResult(
        output_path=destination,
        video_codec_mode="copy",
        audio_codec="aac",
        audio_bitrate=audio_bitrate,
    )



def find_ffprobe_executable(
    ffprobe_path: str | Path | None = None,
    *,
    ffmpeg_path: str | Path | None = None,
) -> str:
    """Find FFprobe beside FFmpeg, on PATH, or in common bundled locations."""
    candidates: list[str | Path] = []
    if ffprobe_path is not None:
        candidates.append(ffprobe_path)
    environment_path = os.environ.get("FFPROBE_BINARY")
    if environment_path:
        candidates.append(environment_path)

    ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    try:
        ffmpeg_executable = find_ffmpeg_executable(ffmpeg_path)
    except MediaToolError:
        ffmpeg_executable = None
    if ffmpeg_executable:
        candidates.append(Path(ffmpeg_executable).with_name(ffprobe_name))

    located = shutil.which(ffprobe_name)
    if located:
        candidates.append(located)

    module_root = Path(__file__).resolve().parent.parent
    directories = [
        Path(sys.executable).resolve().parent,
        module_root / "bin",
        module_root / "tools" / "ffmpeg" / "bin",
        module_root / "build",
        module_root / "build" / "Release",
        module_root / "vcpkg" / "installed" / "x64-windows" / "tools" / "ffmpeg",
        module_root / "vcpkg_installed" / "x64-windows" / "tools" / "ffmpeg",
        module_root / "vcpkg_installed" / "x64-windows" / "tools" / "ffmpeg" / "bin",
    ]
    for directory in directories:
        candidates.append(directory / ffprobe_name)

    for candidate in candidates:
        candidate_text = str(candidate)
        located_candidate = shutil.which(candidate_text)
        if located_candidate:
            return str(Path(located_candidate).resolve())
        path = Path(candidate_text).expanduser()
        if path.is_file():
            return str(path.resolve())

    if ffprobe_path is not None:
        raise MediaToolError(f"指定されたFFprobe実行ファイルが見つかりません: {ffprobe_path}")
    raise MediaToolError(
        "FFprobe実行ファイルが見つかりません。通常はFFmpegと同じ配布物に含まれます。"
        "FFPROBE_BINARY に実行ファイルのパスを設定することもできます。"
    )


def probe_video_dimensions(
    video_path: str | Path,
    *,
    ffprobe_path: str | Path | None = None,
    ffmpeg_path: str | Path | None = None,
) -> tuple[int, int]:
    """Read the first video stream dimensions using FFprobe JSON output."""
    source = Path(video_path).expanduser().resolve()
    if not source.is_file():
        raise MediaToolError(f"動画素材が見つかりません: {source}")
    executable = find_ffprobe_executable(ffprobe_path, ffmpeg_path=ffmpeg_path)
    command = [
        executable,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(source),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise MediaToolError(f"FFprobeを起動できませんでした: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise MediaToolError(
            f"動画の解像度を取得できませんでした: {source}"
            + (f"\n{detail}" if detail else "")
        )
    try:
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams", [])
        stream = streams[0] if streams else {}
        width = int(stream["width"])
        height = int(stream["height"])
    except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise MediaToolError(f"動画の解像度情報が不正です: {source}") from exc
    if width <= 0 or height <= 0:
        raise MediaToolError(f"動画の解像度が不正です: {width}x{height}")
    return width, height

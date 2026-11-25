"""Sample-accurate timeline audio mixdown for PCM WAV project assets.

This module is deliberately independent from Qt and the VO-SE engine. It renders
audio/voice clips into a separate stereo PCM WAV file and never changes source
assets or the existing synthesis/export paths.

Supported inputs are uncompressed integer PCM WAV files with 1 or 2 channels
and 8, 16, 24, or 32 bits per sample. Other formats should be transcoded to PCM
WAV before using this first renderer version.
"""
from __future__ import annotations

import math
import os
import tempfile
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

from media_ffmpeg import MediaToolError, decode_audio_clip_to_f32le
from timeline_model import TimelineClip, TimelineModel


class AudioRenderError(ValueError):
    """Raised when a timeline audio mix cannot be rendered safely."""


@dataclass(frozen=True)
class AudioRenderResult:
    output_path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    audio_clip_count: int
    peak_level: float
    clipped_sample_count: int


@dataclass
class _WaveSource:
    reader: wave.Wave_read
    path: Path
    channels: int
    sample_width: int
    sample_rate: int
    frame_count: int


@dataclass
class _F32Source:
    reader: BinaryIO
    path: Path
    sample_rate: int
    frame_count: int


@dataclass
class _PreparedClip:
    clip: TimelineClip
    source: _WaveSource | _F32Source
    destination_start: int
    frame_count: int


def timeline_audio_clips(model: TimelineModel) -> list[TimelineClip]:
    """Return enabled, unmuted audio and voice clips in stable timeline order."""
    tracks = {track.id: track for track in model.tracks}
    clips = [
        clip for clip in model.clips
        if clip.kind in {"audio", "voice"}
        and clip.enabled
        and (track := tracks.get(clip.track_id)) is not None
        and track.kind == "audio"
        and not track.muted
    ]
    return sorted(clips, key=lambda clip: (clip.start, clip.track_id, clip.id))


def _decode_pcm(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    frame_bytes = sample_width * channels
    usable_bytes = len(raw) - (len(raw) % frame_bytes)
    if usable_bytes == 0:
        return np.zeros((0, channels), dtype=np.float32)
    raw = raw[:usable_bytes]

    if sample_width == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        values = (values - 128.0) / 128.0
    elif sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
        values = integers.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioRenderError(f"Unsupported PCM sample width: {sample_width} bytes")

    return values.reshape(-1, channels)


def _open_wave_source(path: Path) -> _WaveSource:
    try:
        reader = wave.open(str(path), "rb")
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioRenderError(f"Could not read WAV asset {path}: {exc}") from exc

    try:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        compression = reader.getcomptype()
        if compression != "NONE":
            raise AudioRenderError(
                f"Compressed WAV is not supported: {path} ({compression})"
            )
        if channels not in {1, 2}:
            raise AudioRenderError(
                f"Only mono and stereo WAV assets are supported: {path} ({channels} channels)"
            )
        if sample_width not in {1, 2, 3, 4}:
            raise AudioRenderError(
                f"Only 8/16/24/32-bit PCM WAV assets are supported: {path}"
            )
        if sample_rate <= 0 or frame_count < 0:
            raise AudioRenderError(f"Invalid WAV stream metadata: {path}")
        return _WaveSource(
            reader=reader,
            path=path,
            channels=channels,
            sample_width=sample_width,
            sample_rate=sample_rate,
            frame_count=frame_count,
        )
    except Exception:
        reader.close()
        raise


def _open_f32_source(path: Path, sample_rate: int) -> _F32Source:
    reader = path.open("rb")
    frame_count = path.stat().st_size // 8
    if frame_count <= 0:
        reader.close()
        raise AudioRenderError(f"Decoded audio is empty: {path}")
    return _F32Source(
        reader=reader,
        path=path,
        sample_rate=sample_rate,
        frame_count=frame_count,
    )


def _read_resampled_stereo(
    source: _WaveSource | _F32Source,
    clip: TimelineClip,
    local_output_start: int,
    output_frame_count: int,
    output_sample_rate: int,
) -> np.ndarray:
    """Read, convert to stereo float32 and resample one clip segment."""
    if output_frame_count <= 0 or source.frame_count == 0:
        return np.zeros((max(0, output_frame_count), 2), dtype=np.float32)

    if isinstance(source, _F32Source):
        # FFmpeg has already applied source_in and converted this clip to the
        # output rate and stereo float32; random-access reads stay bounded.
        source.reader.seek(local_output_start * 8)
        raw = source.reader.read(output_frame_count * 8)
        decoded = np.frombuffer(raw[: len(raw) - (len(raw) % 8)], dtype="<f4")
        frames = decoded.reshape(-1, 2)
        if len(frames) < output_frame_count:
            padded = np.zeros((output_frame_count, 2), dtype=np.float32)
            padded[:len(frames)] = frames
            return padded
        return frames.astype(np.float32, copy=False)
        return np.zeros((max(0, output_frame_count), 2), dtype=np.float32)

    output_indices = np.arange(
        local_output_start,
        local_output_start + output_frame_count,
        dtype=np.float64,
    )
    source_positions = (
        clip.source_in * source.sample_rate
        + output_indices * source.sample_rate / output_sample_rate
    )
    first_source_frame = max(0, int(math.floor(float(source_positions[0]))))
    if first_source_frame >= source.frame_count:
        return np.zeros((output_frame_count, 2), dtype=np.float32)

    last_source_frame = int(math.floor(float(source_positions[-1]))) + 2
    frames_to_read = min(source.frame_count, last_source_frame) - first_source_frame
    source.reader.setpos(first_source_frame)
    raw = source.reader.readframes(frames_to_read)
    decoded = _decode_pcm(raw, source.sample_width, source.channels)
    if decoded.shape[0] == 0:
        return np.zeros((output_frame_count, 2), dtype=np.float32)

    relative_positions = source_positions - first_source_frame
    source_axis = np.arange(decoded.shape[0], dtype=np.float64)
    resampled = np.empty((output_frame_count, source.channels), dtype=np.float32)
    for channel in range(source.channels):
        resampled[:, channel] = np.interp(
            relative_positions,
            source_axis,
            decoded[:, channel],
            left=0.0,
            right=0.0,
        ).astype(np.float32)

    if source.channels == 1:
        return np.repeat(resampled, 2, axis=1)
    return resampled


def _clip_envelope(clip: TimelineClip, local_frames: np.ndarray, sample_rate: int) -> np.ndarray:
    elapsed = local_frames.astype(np.float64) / sample_rate
    remaining = np.maximum(0.0, clip.duration - elapsed)
    envelope = np.full(local_frames.shape, clip.volume, dtype=np.float32)
    if clip.fade_in > 0.0:
        envelope *= np.minimum(1.0, elapsed / clip.fade_in).astype(np.float32)
    if clip.fade_out > 0.0:
        envelope *= np.minimum(1.0, remaining / clip.fade_out).astype(np.float32)
    return envelope


def render_timeline_audio(
    model: TimelineModel,
    asset_resolver: Callable[[TimelineClip], str | Path],
    output_path: str | Path,
    *,
    sample_rate: int = 48_000,
    duration_seconds: float | None = None,
    block_seconds: float = 1.0,
    progress_callback: Callable[[int, int], None] | None = None,
    ffmpeg_path: str | Path | None = None,
) -> AudioRenderResult:
    """Mix all enabled/unmuted timeline audio clips into a stereo PCM16 WAV.

    The renderer sums overlapping clips in floating point, applies each clip's
    gain and edge fades, then clips the final mix to the PCM16 range. It writes
    to a temporary sibling file and atomically replaces the destination only
    after a complete successful render.
    """
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise AudioRenderError("Output sample rate must be an integer")
    if not 8_000 <= sample_rate <= 192_000:
        raise AudioRenderError("Output sample rate must be between 8000 and 192000 Hz")
    try:
        block_seconds_value = float(block_seconds)
    except (TypeError, ValueError) as exc:
        raise AudioRenderError("Render block size must be a number") from exc
    if not math.isfinite(block_seconds_value) or block_seconds_value <= 0.0:
        raise AudioRenderError("Render block size must be greater than zero")

    clips = timeline_audio_clips(model)
    if not clips:
        raise AudioRenderError("There are no enabled, unmuted audio clips to render")

    if duration_seconds is None:
        render_duration = model.duration_sec
    else:
        try:
            render_duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise AudioRenderError("Output duration must be a number") from exc
    if not math.isfinite(render_duration) or render_duration <= 0.0:
        raise AudioRenderError("Output duration must be finite and greater than zero")

    total_frames = int(round(render_duration * sample_rate))
    if total_frames <= 0:
        raise AudioRenderError("Output duration is shorter than one output sample")
    block_frames = max(1, int(round(block_seconds_value * sample_rate)))

    destination = Path(output_path).expanduser()
    if destination.suffix.lower() != ".wav":
        raise AudioRenderError("Timeline audio mix output must use the .wav extension")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AudioRenderError(f"Could not create output folder: {exc}") from exc

    prepared: list[_PreparedClip] = []
    temp_path: Path | None = None
    peak_level = 0.0
    clipped_sample_count = 0
    sources: list[_WaveSource | _F32Source] = []
    decode_temp_dir: Path | None = None

    try:
        for clip in clips:
            try:
                resolved = Path(asset_resolver(clip)).expanduser().resolve()
            except Exception as exc:
                raise AudioRenderError(
                    f"Could not resolve audio asset for clip '{clip.name}': {exc}"
                ) from exc
            if resolved == destination.resolve():
                raise AudioRenderError(
                    f"Output must not overwrite source audio asset: {resolved}"
                )
            if not resolved.is_file():
                raise AudioRenderError(
                    f"Audio asset for clip '{clip.name}' does not exist: {resolved}"
                )
            try:
                source: _WaveSource | _F32Source = _open_wave_source(resolved)
            except AudioRenderError as wave_error:
                try:
                    if decode_temp_dir is None:
                        decode_temp_dir = Path(
                            tempfile.mkdtemp(prefix="aural-audio-decode-")
                        )
                    decoded_path = decode_temp_dir / f"{uuid.uuid4().hex}.f32le"
                    decode_audio_clip_to_f32le(
                        resolved,
                        decoded_path,
                        source_in=clip.source_in,
                        duration=clip.duration,
                        sample_rate=sample_rate,
                        ffmpeg_path=ffmpeg_path,
                    )
                    source = _open_f32_source(decoded_path, sample_rate)
                except (MediaToolError, OSError) as decode_error:
                    raise AudioRenderError(
                        f"「{clip.name}」の音声形式を読み込めません。"
                        f"PCM WAV以外の形式にはFFmpegが必要です。"
                        f"\nWAV reader: {wave_error}\nFFmpeg: {decode_error}"
                    ) from decode_error
            sources.append(source)
            prepared.append(
                _PreparedClip(
                    clip=clip,
                    source=source,
                    destination_start=int(round(clip.start * sample_rate)),
                    frame_count=max(1, int(round(clip.duration * sample_rate))),
                )
            )

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".tmp.wav",
            dir=str(destination.parent),
        )
        os.close(fd)
        temp_path = Path(temp_name)

        with wave.open(str(temp_path), "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)

            for block_start in range(0, total_frames, block_frames):
                block_end = min(total_frames, block_start + block_frames)
                block_count = block_end - block_start
                mixed = np.zeros((block_count, 2), dtype=np.float32)

                for prepared_clip in prepared:
                    clip_start = prepared_clip.destination_start
                    clip_end = clip_start + prepared_clip.frame_count
                    overlap_start = max(block_start, clip_start)
                    overlap_end = min(block_end, clip_end)
                    if overlap_start >= overlap_end:
                        continue

                    local_start = overlap_start - clip_start
                    overlap_count = overlap_end - overlap_start
                    audio = _read_resampled_stereo(
                        prepared_clip.source,
                        prepared_clip.clip,
                        local_start,
                        overlap_count,
                        sample_rate,
                    )
                    local_frames = np.arange(
                        local_start,
                        local_start + overlap_count,
                        dtype=np.int64,
                    )
                    envelope = _clip_envelope(
                        prepared_clip.clip, local_frames, sample_rate
                    )
                    destination_offset = overlap_start - block_start
                    mixed[destination_offset:destination_offset + overlap_count] += (
                        audio * envelope[:, np.newaxis]
                    )

                peak_level = max(
                    peak_level,
                    float(np.max(np.abs(mixed))) if mixed.size else 0.0,
                )
                clipped_sample_count += int(np.count_nonzero(np.abs(mixed) > 1.0))
                limited = np.clip(mixed, -1.0, 1.0)
                pcm = np.where(
                    limited < 0.0,
                    np.rint(limited * 32768.0),
                    np.rint(limited * 32767.0),
                ).astype("<i2")
                writer.writeframesraw(pcm.tobytes())

                if progress_callback is not None:
                    progress_callback(block_end, total_frames)

        # Close all input readers before atomically replacing the destination.
        for source in sources:
            source.reader.close()
        sources.clear()
        os.replace(temp_path, destination)
        temp_path = None

        return AudioRenderResult(
            output_path=destination.resolve(),
            duration_seconds=total_frames / sample_rate,
            sample_rate=sample_rate,
            channels=2,
            audio_clip_count=len(prepared),
            peak_level=peak_level,
            clipped_sample_count=clipped_sample_count,
        )
    except AudioRenderError:
        raise
    except (OSError, wave.Error, EOFError, ValueError) as exc:
        raise AudioRenderError(f"Timeline audio render failed: {exc}") from exc
    finally:
        for source in sources:
            try:
                source.reader.close()
            except Exception:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if decode_temp_dir is not None:
            import shutil
            shutil.rmtree(decode_temp_dir, ignore_errors=True)

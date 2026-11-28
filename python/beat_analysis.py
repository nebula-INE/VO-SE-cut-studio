"""Lightweight BPM and beat-phase analysis for local audio assets.

The detector uses a spectral-flux onset envelope and FFT autocorrelation. It has
no network or machine-learning dependency and limits analysis to a bounded
window so long source files do not cause unbounded memory use.
"""
from __future__ import annotations

import math
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from media_ffmpeg import MediaToolError, find_ffmpeg_executable


ANALYSIS_SAMPLE_RATE = 11_025
MAX_ANALYSIS_SECONDS = 180.0
_FRAME_SIZE = 2_048
_HOP_SIZE = 256


class BeatAnalysisError(ValueError):
    """Raised when an audio asset cannot be analyzed for a stable beat."""


@dataclass(frozen=True)
class BeatAnalysisResult:
    bpm: float
    beat_offset_seconds: float
    confidence: float
    analyzed_duration_seconds: float


def _decode_pcm(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    if channels < 1:
        raise BeatAnalysisError("音声チャンネル数が不正です")
    if sample_width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32_768.0
    elif sample_width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8)
        octets = octets[: len(octets) - len(octets) % 3].reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
        values = integers.astype(np.float32) / 8_388_608.0
    elif sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2_147_483_648.0
    else:
        raise BeatAnalysisError(
            f"未対応のWAV PCM形式です（{sample_width * 8} bit）"
        )

    usable = len(values) - len(values) % channels
    if usable == 0:
        return np.zeros(0, dtype=np.float32)
    return values[:usable].reshape(-1, channels).mean(axis=1, dtype=np.float32)


def _read_pcm_wave(
    path: Path,
    *,
    start_seconds: float,
    duration_seconds: float | None,
) -> tuple[np.ndarray, int, float] | None:
    """Return mono PCM samples, sample rate, and the actual segment start.

    Return None when the WAV is compressed or not a supported PCM container so
    the caller can try the already-supported FFmpeg decoder.
    """
    try:
        reader = wave.open(str(path), "rb")
    except (wave.Error, EOFError):
        return None
    except OSError as exc:
        raise BeatAnalysisError(f"音声ファイルを開けません: {exc}") from exc

    with reader:
        if reader.getcomptype() != "NONE":
            return None
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        total_frames = reader.getnframes()
        if sample_rate < 8_000 or total_frames <= 0:
            raise BeatAnalysisError("音声のサンプルレートまたは長さが不正です")

        start_frame = int(round(start_seconds * sample_rate))
        if start_frame >= total_frames:
            raise BeatAnalysisError("解析開始位置が音声の長さを超えています")
        available = total_frames - start_frame
        frame_limit = int(math.ceil(MAX_ANALYSIS_SECONDS * sample_rate))
        if duration_seconds is not None:
            frame_limit = min(frame_limit, int(math.ceil(duration_seconds * sample_rate)))
        frame_count = min(available, frame_limit)
        if frame_count <= 0:
            raise BeatAnalysisError("解析対象の音声区間が空です")

        reader.setpos(start_frame)
        raw = reader.readframes(frame_count)
        mono = _decode_pcm(raw, sample_width, channels)
        actual_start = start_frame / sample_rate
        return mono, sample_rate, actual_start


def _read_with_ffmpeg(
    path: Path,
    *,
    start_seconds: float,
    duration_seconds: float | None,
    ffmpeg_path: str | Path | None,
) -> tuple[np.ndarray, int, float]:
    try:
        executable = find_ffmpeg_executable(ffmpeg_path)
    except MediaToolError as exc:
        raise BeatAnalysisError(
            "この音声形式のBPM解析にはFFmpegが必要です。FFmpegを設定するか、"
            "非圧縮PCM WAVを使用してください。"
        ) from exc

    duration = min(
        MAX_ANALYSIS_SECONDS,
        duration_seconds if duration_seconds is not None else MAX_ANALYSIS_SECONDS,
    )
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(path),
        "-ss", f"{start_seconds:.6f}",
        "-t", f"{duration:.6f}",
        "-map", "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac", "1",
        "-ar", str(ANALYSIS_SAMPLE_RATE),
        "-f", "f32le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BeatAnalysisError(f"FFmpegで音声を読み込めませんでした: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BeatAnalysisError(
            "音声をデコードできませんでした"
            + (f"\n{detail}" if detail else "")
        )
    usable = len(completed.stdout) - len(completed.stdout) % 4
    mono = np.frombuffer(completed.stdout[:usable], dtype="<f4").astype(np.float32)
    if mono.size == 0:
        raise BeatAnalysisError("解析対象の音声データがありません")
    mono = np.nan_to_num(mono, copy=False)
    return mono, ANALYSIS_SAMPLE_RATE, start_seconds


def _onset_envelope(samples: np.ndarray, sample_rate: float) -> tuple[np.ndarray, float]:
    if samples.size < _FRAME_SIZE * 3:
        raise BeatAnalysisError("BPM解析には3秒以上の音声が必要です")
    if not np.all(np.isfinite(samples)):
        samples = np.nan_to_num(samples, copy=True)
    if float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) < 1e-5:
        raise BeatAnalysisError("音量が小さすぎるため拍を検出できません")

    window = np.hanning(_FRAME_SIZE).astype(np.float32)
    flux: list[float] = []
    previous: np.ndarray | None = None
    for start in range(0, samples.size - _FRAME_SIZE + 1, _HOP_SIZE):
        frame = samples[start : start + _FRAME_SIZE] * window
        magnitude = np.abs(np.fft.rfft(frame))
        magnitude[0] = 0.0
        spectrum = np.log1p(magnitude)
        if previous is not None:
            flux.append(float(np.maximum(spectrum - previous, 0.0).sum()))
        previous = spectrum

    envelope = np.asarray(flux, dtype=np.float64)
    if envelope.size < 16:
        raise BeatAnalysisError("BPM解析に必要な音声区間が短すぎます")
    envelope = np.convolve(envelope, np.ones(3, dtype=np.float64) / 3.0, mode="same")
    envelope = np.maximum(envelope - np.median(envelope), 0.0)
    if float(np.std(envelope)) < 1e-8:
        raise BeatAnalysisError("明確な拍を検出できませんでした")
    return envelope, sample_rate / _HOP_SIZE


def analyze_audio_file(
    path: str | Path,
    *,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    min_bpm: float = 60.0,
    max_bpm: float = 200.0,
    ffmpeg_path: str | Path | None = None,
) -> BeatAnalysisResult:
    """Estimate tempo and the first beat phase in an audio source segment.

    The returned beat_offset_seconds is in the original asset's timebase,
    including start_seconds, so it can be mapped directly onto a trimmed
    timeline clip.
    """
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise BeatAnalysisError(f"音声ファイルが見つかりません: {source}")
    try:
        start_value = float(start_seconds)
        duration_value = None if duration_seconds is None else float(duration_seconds)
        minimum = float(min_bpm)
        maximum = float(max_bpm)
    except (TypeError, ValueError) as exc:
        raise BeatAnalysisError("BPM解析の範囲指定が不正です") from exc
    if not math.isfinite(start_value) or start_value < 0:
        raise BeatAnalysisError("解析開始位置は0以上の有限値を指定してください")
    if duration_value is not None and (
        not math.isfinite(duration_value) or duration_value <= 0
    ):
        raise BeatAnalysisError("解析区間の長さは0より大きい有限値を指定してください")
    if (
        not math.isfinite(minimum)
        or not math.isfinite(maximum)
        or minimum < 40.0
        or maximum > 240.0
        or minimum >= maximum
    ):
        raise BeatAnalysisError("BPM解析範囲は40〜240 BPMで指定してください")

    loaded = _read_pcm_wave(
        source,
        start_seconds=start_value,
        duration_seconds=duration_value,
    )
    if loaded is None:
        mono, sample_rate, actual_start = _read_with_ffmpeg(
            source,
            start_seconds=start_value,
            duration_seconds=duration_value,
            ffmpeg_path=ffmpeg_path,
        )
    else:
        mono, sample_rate, actual_start = loaded

    # Average adjacent samples while downsampling. This keeps analysis bounded
    # and avoids simply dropping samples from common 44.1/48 kHz source files.
    factor = max(1, int(round(sample_rate / ANALYSIS_SAMPLE_RATE)))
    usable = mono.size - mono.size % factor
    if usable == 0:
        raise BeatAnalysisError("解析対象の音声データがありません")
    if factor > 1:
        mono = mono[:usable].reshape(-1, factor).mean(axis=1, dtype=np.float32)
    effective_rate = sample_rate / factor

    envelope, onset_rate = _onset_envelope(mono, effective_rate)
    centered = envelope - float(np.mean(envelope))
    fft_size = 1 << (2 * centered.size - 1).bit_length()
    spectrum = np.fft.rfft(centered, n=fft_size)
    autocorrelation = np.fft.irfft(spectrum * np.conjugate(spectrum), n=fft_size)[: centered.size]
    autocorrelation /= np.maximum(1.0, centered.size - np.arange(centered.size))
    if autocorrelation[0] <= 1e-12:
        raise BeatAnalysisError("明確な拍を検出できませんでした")
    normalized = autocorrelation / autocorrelation[0]

    min_lag = max(2, int(math.floor(onset_rate * 60.0 / maximum)))
    max_lag = min(
        centered.size // 2,
        int(math.ceil(onset_rate * 60.0 / minimum)),
    )
    if max_lag <= min_lag:
        raise BeatAnalysisError("BPM解析に必要な音声区間が短すぎます")
    candidate_lags = np.arange(min_lag, max_lag + 1)
    candidate_correlations = normalized[min_lag : max_lag + 1]
    # A single transient every half-second is mathematically ambiguous between
    # 60 and 120 BPM. Apply a mild musical-tempo prior so strong half-time
    # harmonics do not systematically force the slower interpretation.
    candidate_bpms = 60.0 * onset_rate / candidate_lags
    candidate_scores = candidate_correlations * np.power(candidate_bpms / 120.0, 0.2)
    best_lag = min_lag + int(np.argmax(candidate_scores))
    lag_refined = float(best_lag)
    if min_lag < best_lag < max_lag:
        left, center, right = normalized[best_lag - 1 : best_lag + 2]
        denominator = float(left - 2.0 * center + right)
        if abs(denominator) > 1e-12:
            adjustment = 0.5 * float(left - right) / denominator
            lag_refined += min(0.5, max(-0.5, adjustment))

    bpm = 60.0 * onset_rate / lag_refined
    phase_lag = max(1, int(round(lag_refined)))
    phase_scores = np.bincount(
        np.arange(envelope.size) % phase_lag,
        weights=envelope,
        minlength=phase_lag,
    )
    phase_index = int(np.argmax(phase_scores))
    # The onset envelope index marks the first analyzed FFT hop; translate
    # its frame-start phase back to the center of the corresponding window.
    beat_offset = actual_start + (
        phase_index * _HOP_SIZE + _FRAME_SIZE / 2.0 + _HOP_SIZE
    ) / effective_rate
    peak_score = float(normalized[best_lag])
    mean_score = float(np.mean(candidate_correlations))
    confidence = min(1.0, max(0.0, (peak_score - mean_score) / max(peak_score, 1e-12)))

    return BeatAnalysisResult(
        bpm=float(min(max(bpm, minimum), maximum)),
        beat_offset_seconds=float(beat_offset),
        confidence=float(confidence),
        analyzed_duration_seconds=float(mono.size / effective_rate),
    )

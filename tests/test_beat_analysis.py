from __future__ import annotations

import wave

import numpy as np
import pytest

from beat_analysis import BeatAnalysisError, analyze_audio_file


def _write_click_track(path, *, bpm: float = 120.0, offset: float = 0.31, duration: float = 24.0):
    sample_rate = 22_050
    samples = np.zeros(int(sample_rate * duration), dtype=np.float32)
    interval = 60.0 / bpm
    cursor = offset
    pulse_length = int(0.045 * sample_rate)
    pulse_time = np.arange(pulse_length, dtype=np.float32) / sample_rate
    pulse = 0.85 * np.sin(2.0 * np.pi * 880.0 * pulse_time) * np.exp(-pulse_time * 18.0)
    while cursor < duration:
        start = int(round(cursor * sample_rate))
        end = min(samples.size, start + pulse.size)
        if end > start:
            samples[start:end] += pulse[: end - start]
        cursor += interval
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32_767.0).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def test_analyze_audio_file_detects_tempo_and_beat_phase(tmp_path):
    source = tmp_path / "clicks.wav"
    _write_click_track(source)

    result = analyze_audio_file(source)

    assert result.bpm == pytest.approx(120.0, abs=4.0)
    phase_error = abs((result.beat_offset_seconds - 0.31 + 0.25) % 0.5 - 0.25)
    assert phase_error < 0.16
    assert 0.0 <= result.confidence <= 1.0
    assert result.analyzed_duration_seconds <= 180.0


def test_analyze_audio_file_uses_source_time_for_trimmed_clip_phase(tmp_path):
    source = tmp_path / "clicks.wav"
    _write_click_track(source, duration=20.0)

    result = analyze_audio_file(
        source,
        start_seconds=2.0,
        duration_seconds=8.0,
    )

    assert result.bpm == pytest.approx(120.0, abs=4.0)
    assert 2.0 <= result.beat_offset_seconds < 2.5


def test_analyze_audio_file_rejects_silence(tmp_path):
    source = tmp_path / "silence.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(22_050)
        output.writeframes(np.zeros(22_050 * 8, dtype="<i2").tobytes())

    with pytest.raises(BeatAnalysisError, match="音量が小さすぎ"):
        analyze_audio_file(source)


def test_analyze_audio_file_rejects_missing_and_invalid_ranges(tmp_path):
    with pytest.raises(BeatAnalysisError, match="見つかりません"):
        analyze_audio_file(tmp_path / "missing.wav")

    source = tmp_path / "clicks.wav"
    _write_click_track(source)
    with pytest.raises(BeatAnalysisError, match="解析開始位置"):
        analyze_audio_file(source, start_seconds=-1.0)
    with pytest.raises(BeatAnalysisError, match="BPM解析範囲"):
        analyze_audio_file(source, min_bpm=220.0, max_bpm=80.0)

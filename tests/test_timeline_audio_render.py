from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from timeline_audio_render import (
    AudioRenderError,
    render_timeline_audio,
)
from timeline_model import TimelineModel


def _write_pcm_wav(path: Path, samples: np.ndarray, sample_rate: int = 8_000) -> Path:
    values = np.asarray(samples, dtype=np.float32)
    if values.ndim == 1:
        values = values[:, np.newaxis]
    values = np.clip(values, -1.0, 1.0)
    pcm = np.where(
        values < 0.0,
        np.rint(values * 32768.0),
        np.rint(values * 32767.0),
    ).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(values.shape[1])
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return path


def _read_stereo_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as source:
        assert source.getnchannels() == 2
        assert source.getsampwidth() == 2
        rate = source.getframerate()
        frames = source.getnframes()
        data = np.frombuffer(source.readframes(frames), dtype="<i2")
    return rate, data.reshape(-1, 2).astype(np.float32) / 32768.0


def _audio_timeline():
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    return model, track


def test_mixdown_places_and_sums_overlapping_clips_sample_accurately(tmp_path):
    first_path = _write_pcm_wav(tmp_path / "first.wav", np.full(1_600, 0.25))
    second_path = _write_pcm_wav(tmp_path / "second.wav", np.full(1_600, 0.5))
    model, first_track = _audio_timeline()
    second_track = model.add_track("Music 2", "audio")
    first = model.add_clip(
        first_track.id, "audio", "first", 0.2, start=0.0, asset_id="first"
    )
    second = model.add_clip(
        second_track.id, "audio", "second", 0.2, start=0.1, asset_id="second"
    )
    paths = {first.id: first_path, second.id: second_path}
    output = tmp_path / "mix.wav"

    result = render_timeline_audio(
        model, lambda clip: paths[clip.id], output, sample_rate=8_000, block_seconds=0.037
    )
    rate, mixed = _read_stereo_wav(output)

    assert rate == 8_000
    assert mixed.shape == (2_400, 2)
    assert mixed[200, 0] == pytest.approx(0.25, abs=1 / 32768)
    assert mixed[1_000, 0] == pytest.approx(0.75, abs=1 / 32768)
    assert mixed[2_000, 0] == pytest.approx(0.5, abs=1 / 32768)
    assert result.audio_clip_count == 2
    assert result.peak_level == pytest.approx(0.75, abs=1 / 32768)
    assert result.clipped_sample_count == 0


def test_mixdown_applies_volume_fades_and_repeats_mono_to_stereo(tmp_path):
    source_path = _write_pcm_wav(tmp_path / "voice.wav", np.full(1_600, 0.5))
    model, track = _audio_timeline()
    clip = model.add_clip(
        track.id,
        "voice",
        "voice",
        0.2,
        asset_id="voice",
        volume=0.8,
        fade_in=0.1,
        fade_out=0.1,
    )
    output = tmp_path / "faded.wav"

    render_timeline_audio(
        model, lambda _clip: source_path, output, sample_rate=8_000
    )
    _rate, mixed = _read_stereo_wav(output)

    assert mixed[0, 0] == pytest.approx(0.0, abs=1 / 32768)
    assert mixed[400, 0] == pytest.approx(0.2, abs=1 / 32768)
    assert mixed[800, 0] == pytest.approx(0.4, abs=1 / 32768)
    assert mixed[1_200, 0] == pytest.approx(0.2, abs=1 / 32768)
    assert mixed[400, 0] == pytest.approx(mixed[400, 1], abs=1 / 32768)


def test_mixdown_resamples_source_and_respects_source_in(tmp_path):
    # A constant signal is invariant under linear resampling.
    source_path = _write_pcm_wav(
        tmp_path / "resample.wav", np.full(3_200, 0.4), sample_rate=16_000
    )
    model, track = _audio_timeline()
    clip = model.add_clip(
        track.id,
        "audio",
        "resampled",
        0.1,
        source_in=0.05,
        asset_id="resample",
    )
    output = tmp_path / "resampled.wav"

    render_timeline_audio(model, lambda _clip: source_path, output, sample_rate=8_000)
    _rate, mixed = _read_stereo_wav(output)

    assert mixed.shape == (800, 2)
    assert np.allclose(mixed[:, 0], 0.4, atol=1 / 32768)


def test_mixdown_skips_muted_tracks_and_disabled_clips(tmp_path):
    muted_path = _write_pcm_wav(tmp_path / "muted.wav", np.full(800, 0.9))
    disabled_path = _write_pcm_wav(tmp_path / "disabled.wav", np.full(800, 0.8))
    model, track = _audio_timeline()
    muted = model.add_clip(track.id, "audio", "muted", 0.1, asset_id="muted")
    track.muted = True
    disabled_track = model.add_track("Enabled", "audio")
    disabled = model.add_clip(
        disabled_track.id, "audio", "disabled", 0.1, asset_id="disabled"
    )
    disabled.enabled = False
    output = tmp_path / "should-not-exist.wav"

    with pytest.raises(AudioRenderError, match="no enabled, unmuted"):
        render_timeline_audio(
            model,
            lambda clip: {"muted": muted_path, "disabled": disabled_path}[clip.asset_id],
            output,
            sample_rate=8_000,
        )
    assert not output.exists()


def test_mixdown_reports_hard_clipping_without_automatic_normalization(tmp_path):
    first_path = _write_pcm_wav(tmp_path / "hot1.wav", np.full(800, 0.8))
    second_path = _write_pcm_wav(tmp_path / "hot2.wav", np.full(800, 0.8))
    model, first_track = _audio_timeline()
    second_track = model.add_track("Audio 2", "audio")
    first = model.add_clip(first_track.id, "audio", "hot1", 0.1, asset_id="hot1")
    second = model.add_clip(second_track.id, "audio", "hot2", 0.1, asset_id="hot2")
    paths = {first.id: first_path, second.id: second_path}
    output = tmp_path / "clipped.wav"

    result = render_timeline_audio(
        model, lambda clip: paths[clip.id], output, sample_rate=8_000
    )

    assert result.peak_level == pytest.approx(1.6, abs=1 / 32768)
    assert result.clipped_sample_count > 0
    _rate, mixed = _read_stereo_wav(output)
    assert np.max(mixed) == pytest.approx(1.0, abs=1 / 32768)


def test_mixdown_preserves_existing_destination_when_asset_cannot_be_resolved(tmp_path):
    output = tmp_path / "preserve.wav"
    output.write_bytes(b"previous export")
    model, track = _audio_timeline()
    model.add_clip(track.id, "audio", "missing", 0.1, asset_id="missing")

    with pytest.raises(AudioRenderError, match="does not exist"):
        render_timeline_audio(model, lambda _clip: tmp_path / "missing.wav", output, sample_rate=8_000)

    assert output.read_bytes() == b"previous export"


def test_mixdown_supports_pcm24_input(tmp_path):
    path = tmp_path / "pcm24.wav"
    # Little-endian signed 24-bit PCM samples, repeated as mono.
    values = [4_194_304, -4_194_304, 0, 2_097_152]
    payload = bytearray()
    for value in values:
        unsigned = value & 0xFFFFFF
        payload.extend((unsigned & 0xFF, (unsigned >> 8) & 0xFF, (unsigned >> 16) & 0xFF))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(3)
        output.setframerate(8_000)
        output.writeframes(bytes(payload))

    model, track = _audio_timeline()
    model.add_clip(track.id, "audio", "24-bit", 0.05, asset_id="pcm24")
    result = render_timeline_audio(
        model, lambda _clip: path, tmp_path / "pcm24-mix.wav", sample_rate=8_000
    )
    _rate, mixed = _read_stereo_wav(tmp_path / "pcm24-mix.wav")

    assert result.audio_clip_count == 1
    assert mixed[0, 0] == pytest.approx(0.5, abs=1 / 32768)
    assert mixed[1, 0] == pytest.approx(-0.5, abs=1 / 32768)


def test_mixdown_reports_progress_in_monotonic_frame_ranges(tmp_path):
    source_path = _write_pcm_wav(tmp_path / "progress.wav", np.full(800, 0.2))
    model, track = _audio_timeline()
    model.add_clip(track.id, "audio", "progress", 0.1, asset_id="progress")
    progress = []

    render_timeline_audio(
        model,
        lambda _clip: source_path,
        tmp_path / "progress-mix.wav",
        sample_rate=8_000,
        block_seconds=0.025,
        progress_callback=lambda completed, total: progress.append((completed, total)),
    )

    assert progress
    assert progress[-1] == (800, 800)
    assert all(progress[i][0] < progress[i + 1][0] for i in range(len(progress) - 1))

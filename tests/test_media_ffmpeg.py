from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

import media_ffmpeg
from media_ffmpeg import (
    MediaToolError,
    decode_audio_clip_to_f32le,
    find_ffmpeg_executable,
    mux_audio_into_video,
)


def test_find_ffmpeg_executable_accepts_explicit_file(tmp_path, monkeypatch):
    executable = tmp_path / ("ffmpeg.exe" if media_ffmpeg.os.name == "nt" else "ffmpeg")
    executable.write_bytes(b"fake ffmpeg")
    monkeypatch.setattr(media_ffmpeg.shutil, "which", lambda _name: None)

    assert find_ffmpeg_executable(executable) == str(executable.resolve())


def test_find_ffmpeg_executable_reports_missing_runtime(monkeypatch):
    monkeypatch.setattr(media_ffmpeg.shutil, "which", lambda _name: None)

    with pytest.raises(MediaToolError, match="FFMPEG_BINARY"):
        find_ffmpeg_executable()


def test_decode_audio_clip_to_f32le_invokes_ffmpeg_with_segment_and_pcm_settings(
    tmp_path, monkeypatch
):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    source = tmp_path / "voice.mp3"
    source.write_bytes(b"compressed data")
    decoded = tmp_path / "voice.f32le"
    expected = np.asarray([0.1, -0.1, 0.25, -0.25], dtype="<f4").tobytes()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(expected)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(media_ffmpeg.subprocess, "run", fake_run)
    result = decode_audio_clip_to_f32le(
        source,
        decoded,
        source_in=1.25,
        duration=2.5,
        sample_rate=48_000,
        ffmpeg_path=executable,
    )

    command = captured["command"]
    assert result == decoded.resolve()
    assert command[command.index("-ss") + 1] == "1.250000000"
    assert command[command.index("-t") + 1] == "2.500000000"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-f") + 1] == "f32le"
    assert decoded.read_bytes() == expected


def test_decode_audio_clip_rejects_failed_ffmpeg_process(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    source = tmp_path / "bad.flac"
    source.write_bytes(b"invalid")
    monkeypatch.setattr(
        media_ffmpeg.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="unsupported input"
        ),
    )

    with pytest.raises(MediaToolError, match="unsupported input"):
        decode_audio_clip_to_f32le(
            source,
            tmp_path / "bad.f32le",
            source_in=0.0,
            duration=1.0,
            sample_rate=48_000,
            ffmpeg_path=executable,
        )


def test_mux_audio_into_video_copies_video_and_encodes_aac_atomically(
    tmp_path, monkeypatch
):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    video = tmp_path / "composed.mp4"
    audio = tmp_path / "mix.wav"
    output = tmp_path / "final.mp4"
    video.write_bytes(b"video source")
    audio.write_bytes(b"audio source")
    command_seen = {}

    def fake_run(command, **kwargs):
        command_seen["command"] = command
        Path(command[-1]).write_bytes(b"final mp4 bytes")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(media_ffmpeg.subprocess, "run", fake_run)
    result = mux_audio_into_video(
        video, audio, output, ffmpeg_path=executable, audio_bitrate="256k"
    )

    command = command_seen["command"]
    assert result.output_path == output.resolve()
    assert result.video_codec_mode == "copy"
    assert result.audio_codec == "aac"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "256k"
    assert "-shortest" in command
    assert output.read_bytes() == b"final mp4 bytes"
    assert video.read_bytes() == b"video source"
    assert audio.read_bytes() == b"audio source"


def test_mux_failure_preserves_existing_output_and_inputs(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    video = tmp_path / "video.mp4"
    audio = tmp_path / "mix.wav"
    output = tmp_path / "existing.mp4"
    video.write_bytes(b"original video")
    audio.write_bytes(b"original audio")
    output.write_bytes(b"old final")
    monkeypatch.setattr(
        media_ffmpeg.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="codec not supported"
        ),
    )

    with pytest.raises(MediaToolError, match="codec not supported"):
        mux_audio_into_video(video, audio, output, ffmpeg_path=executable)

    assert output.read_bytes() == b"old final"
    assert video.read_bytes() == b"original video"
    assert audio.read_bytes() == b"original audio"
    assert not list(tmp_path.glob(".existing.*.tmp.mp4"))


def test_mux_rejects_output_same_as_video_or_non_mp4(tmp_path):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")

    with pytest.raises(MediaToolError, match="上書き"):
        mux_audio_into_video(video, audio, video, ffmpeg_path=executable)
    with pytest.raises(MediaToolError, match=".mp4"):
        mux_audio_into_video(video, audio, tmp_path / "final.mov", ffmpeg_path=executable)

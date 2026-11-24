from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import timeline_video_render
from timeline_model import TimelineModel
from timeline_video_render import (
    VideoRenderError,
    render_timeline_video,
    timeline_video_clips,
)


def _model_with_video_and_image():
    model = TimelineModel.create_default()
    video_track = next(track for track in model.tracks if track.kind == "video")
    overlay_track = next(track for track in model.tracks if track.kind == "overlay")
    video = model.add_clip(
        video_track.id, "video", "background", 4.0,
        start=0.0, source_in=1.25, asset_id="video-asset",
    )
    image = model.add_clip(
        overlay_track.id, "image", "logo", 2.0,
        start=1.0, asset_id="image-asset",
    )
    return model, video, image


def test_timeline_video_clips_follow_track_order_and_skip_muted_tracks():
    model, video, image = _model_with_video_and_image()
    muted_track = model.add_track("Hidden video", "video")
    muted_track.muted = True
    model.add_clip(muted_track.id, "video", "muted", 3.0, asset_id="muted-asset")

    assert timeline_video_clips(model) == [video, image]


def test_timeline_video_clips_reject_unrendered_text_and_character_clips():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    model.add_clip(
        overlay.id, "text", "subtitle", 1.0, script_line_id="line-1"
    )

    with pytest.raises(VideoRenderError, match="字幕"):
        timeline_video_clips(model)


def test_render_timeline_video_builds_filtered_composite_and_writes_atomically(
    tmp_path, monkeypatch
):
    model, video, image = _model_with_video_and_image()
    video_path = tmp_path / "background.mp4"
    image_path = tmp_path / "logo.png"
    output = tmp_path / "final.mp4"
    video_path.write_bytes(b"video source")
    image_path.write_bytes(b"image source")
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake ffmpeg")
    captured = {}

    monkeypatch.setattr(
        timeline_video_render,
        "probe_video_dimensions",
        lambda *_args, **_kwargs: (1281, 721),
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"rendered mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    result = render_timeline_video(
        model,
        lambda clip: {video.id: video_path, image.id: image_path}[clip.id],
        output,
        ffmpeg_path=executable,
        duration_seconds=4.0,
    )

    command = captured["command"]
    graph = command[command.index("-filter_complex") + 1]
    assert result.output_path == output.resolve()
    assert (result.width, result.height) == (1280, 720)
    assert result.video_clip_count == 2
    assert "trim=start=1.25:duration=4" in graph
    assert "setpts=PTS-STARTPTS+1/TB" in graph
    assert "overlay=" in graph
    assert "lt(t,3)" in graph
    assert "-loop" in command
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-map") + 1] == "[vout]"
    assert output.read_bytes() == b"rendered mp4"
    assert video_path.read_bytes() == b"video source"
    assert image_path.read_bytes() == b"image source"


def test_render_timeline_video_uses_hd_canvas_for_still_only_projects(
    tmp_path, monkeypatch
):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    image = model.add_clip(overlay.id, "image", "still", 1.5, asset_id="image")
    image_path = tmp_path / "still.png"
    image_path.write_bytes(b"image")
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    monkeypatch.setattr(
        timeline_video_render,
        "subprocess_run_unused",
        None,
        raising=False,
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    result = render_timeline_video(
        model,
        lambda _clip: image_path,
        tmp_path / "still.mp4",
        ffmpeg_path=executable,
    )

    assert (result.width, result.height) == (1920, 1080)
    assert result.duration_seconds == 1.5
    assert captured["command"][captured["command"].index("-filter_complex") + 1].startswith(
        "color=c=black:s=1920x1080"
    )


def test_render_timeline_video_rejects_input_overwrite_and_wrong_extension(tmp_path):
    model, video, image = _model_with_video_and_image()
    video_path = tmp_path / "background.mp4"
    image_path = tmp_path / "logo.png"
    video_path.write_bytes(b"video")
    image_path.write_bytes(b"image")
    resolver = lambda clip: {video.id: video_path, image.id: image_path}[clip.id]
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")

    with pytest.raises(VideoRenderError, match="上書き"):
        render_timeline_video(
            model, resolver, video_path, ffmpeg_path=executable
        )
    with pytest.raises(VideoRenderError, match=".mp4"):
        render_timeline_video(
            model, resolver, tmp_path / "output.mov", ffmpeg_path=executable
        )


def test_render_timeline_video_failure_preserves_existing_output(tmp_path, monkeypatch):
    model, video, image = _model_with_video_and_image()
    video_path = tmp_path / "background.mp4"
    image_path = tmp_path / "logo.png"
    output = tmp_path / "existing.mp4"
    video_path.write_bytes(b"video")
    image_path.write_bytes(b"image")
    output.write_bytes(b"old output")
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    monkeypatch.setattr(
        timeline_video_render,
        "probe_video_dimensions",
        lambda *_args, **_kwargs: (640, 480),
    )
    monkeypatch.setattr(
        timeline_video_render.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="libx264 unavailable"
        ),
    )

    with pytest.raises(VideoRenderError, match="libx264 unavailable"):
        render_timeline_video(
            model,
            lambda clip: {video.id: video_path, image.id: image_path}[clip.id],
            output,
            ffmpeg_path=executable,
        )

    assert output.read_bytes() == b"old output"
    assert not list(tmp_path.glob(".existing.*.tmp.mp4"))


def test_render_timeline_video_requires_at_least_one_visible_visual_clip():
    model = TimelineModel.create_default()
    video_track = next(track for track in model.tracks if track.kind == "video")
    video_track.visible = False

    assert timeline_video_clips(model) == []
    with pytest.raises(VideoRenderError, match="有効で表示中"):
        render_timeline_video(model, lambda _clip: "unused.mp4", "output.mp4")


def test_render_timeline_video_rejects_duration_shorter_than_clip(tmp_path):
    model, video, image = _model_with_video_and_image()
    video_path = tmp_path / "background.mp4"
    image_path = tmp_path / "logo.png"
    video_path.write_bytes(b"video")
    image_path.write_bytes(b"image")

    with pytest.raises(VideoRenderError, match="終端より短い"):
        render_timeline_video(
            model,
            lambda clip: {video.id: video_path, image.id: image_path}[clip.id],
            tmp_path / "short.mp4",
            duration_seconds=2.0,
        )

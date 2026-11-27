from __future__ import annotations

import io
import subprocess
from pathlib import Path

from PIL import Image
import pytest

import timeline_video_render
from timeline_model import TimelineModel
from timeline_video_render import (
    VideoRenderError,
    render_timeline_video,
    timeline_motion_clips,
    timeline_video_clips,
    _render_vrm_sequence,
    _character_lane_assignments,
    _motion_entries_for_character,
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


def test_timeline_video_clips_accept_subtitle_text_clips():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    model.add_clip(
        overlay.id, "text", "subtitle", 1.0, script_line_id="line-1"
    )

    assert timeline_video_clips(model)[0].kind == "text"


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

    with pytest.raises(VideoRenderError, match="終端より短く"):
        render_timeline_video(
            model,
            lambda clip: {video.id: video_path, image.id: image_path}[clip.id],
            tmp_path / "short.mp4",
            duration_seconds=2.0,
        )



def test_timeline_motion_clips_require_a_matching_character():
    model = TimelineModel.create_default()
    motion_track = next(track for track in model.tracks if track.kind == "motion")
    model.add_clip(
        motion_track.id, "motion", "body motion", 1.0, asset_id="motion-asset"
    )

    with pytest.raises(VideoRenderError, match="キャラクタークリップ"):
        timeline_motion_clips(model)



def test_timeline_video_clips_resolve_latest_subtitle_text_from_script_line():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    subtitle = model.add_clip(
        overlay.id, "text", "古い字幕", 1.25,
        start=0.5, script_line_id="line-42",
    )

    assert timeline_video_clips(model, {"line-42": "最新の字幕"}) == [subtitle]


def test_render_timeline_video_adds_subtitle_as_timed_overlay(tmp_path, monkeypatch):
    model = TimelineModel.create_default()
    video_track = next(track for track in model.tracks if track.kind == "video")
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    video = model.add_clip(video_track.id, "video", "scene", 3.0, asset_id="video")
    model.add_clip(
        overlay.id, "text", "古いテキスト", 1.5,
        start=0.75, script_line_id="line-a",
    )
    video_path = tmp_path / "scene.mp4"
    video_path.write_bytes(b"video")
    output = tmp_path / "with-subtitles.mp4"
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake ffmpeg")
    captured = {}

    monkeypatch.setattr(
        timeline_video_render,
        "probe_video_dimensions",
        lambda *_args, **_kwargs: (1280, 720),
    )

    def fake_subtitle(text, width, height, path, *, speaker_color=None):
        path = Path(path)
        path.write_text(text, encoding="utf-8")
        captured["subtitle"] = (text, width, height, path)
        captured["speaker_color"] = speaker_color
        return path

    monkeypatch.setattr(timeline_video_render, "render_subtitle_png", fake_subtitle)

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    result = render_timeline_video(
        model,
        lambda _clip: video_path,
        output,
        subtitle_texts={"line-a": "最新テキスト"},
        subtitle_colors={"line-a": "#D94A4A"},
        ffmpeg_path=executable,
    )

    graph = captured["command"][captured["command"].index("-filter_complex") + 1]
    assert captured["subtitle"][:3] == ("最新テキスト", 1280, 720)
    assert captured["speaker_color"] == "#D94A4A"
    assert "setpts=PTS-STARTPTS+0.75/TB" in graph
    assert "gte(t,0.75)" in graph
    assert "lt(t,2.25)" in graph
    assert result.video_clip_count == 1
    assert result.subtitle_clip_count == 1






def test_timeline_motion_clip_drives_builtin_character_mp4_frames(
    tmp_path, monkeypatch
):
    import json
    from PIL import Image

    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    motion_track = next(track for track in model.tracks if track.kind == "motion")
    model.add_clip(
        overlay.id, "character", "2D Character", 1.0,
        script_line_id="__aural_builtin_2d_character__",
    )
    motion = model.add_clip(
        motion_track.id, "motion", "head motion", 1.0, asset_id="motion-asset"
    )
    motion_path = tmp_path / "actor.motion.json"
    motion_path.write_text(
        json.dumps({
            "format": "aural-motion",
            "version": 1,
            "keyframes": [
                {"time": 0.0, "head": {"tilt_deg": 0.0}},
                {"time": 1.0, "head": {"tilt_deg": 20.0}},
            ],
        }),
        encoding="utf-8",
    )
    output = tmp_path / "character.mp4"
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake ffmpeg")
    captured = {"states": []}

    def fake_character(**kwargs):
        captured["states"].append(kwargs)
        return Image.new("RGBA", (40, 60), (255, 0, 0, 128))

    monkeypatch.setattr(
        timeline_video_render, "render_character_frame", fake_character
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    result = render_timeline_video(
        model,
        lambda clip: motion_path if clip.id == motion.id else tmp_path / "unused",
        output,
        ffmpeg_path=executable,
        still_image_canvas=(1280, 720),
    )

    graph = captured["command"][captured["command"].index("-filter_complex") + 1]
    assert len(captured["states"]) == 30
    assert captured["states"][0]["head_tilt_deg"] == pytest.approx(0.0)
    assert captured["states"][-1]["head_tilt_deg"] > 19.0
    assert "-framerate" in captured["command"]
    assert "overlay=x=21:y=H-h-21" in graph
    assert result.character_clip_count == 1
    assert result.motion_clip_count == 1
    assert result.output_path == output.resolve()


def test_timeline_video_clips_reject_external_3d_character_assets():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    model.add_clip(
        overlay.id, "character", "avatar.vrm", 1.0, asset_id="vrm-asset"
    )

    with pytest.raises(VideoRenderError, match="3D character asset"):
        timeline_video_clips(model)



def _transparent_png_bytes(size=(12, 12)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 0)).save(stream, format="PNG")
    return stream.getvalue()


def test_timeline_video_clips_requires_a_renderer_for_vrm_assets():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    clip = model.add_clip(
        overlay.id, "character", "VRM avatar", 1.0, asset_id="vrm-asset"
    )

    with pytest.raises(VideoRenderError, match="Qt WebEngine"):
        timeline_video_clips(model)

    assert timeline_video_clips(model, allow_vrm=True) == [clip]


def test_render_timeline_video_renders_vrm_frames_to_transparent_sequence(
    tmp_path, monkeypatch
):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    model.add_clip(
        overlay.id, "character", "VRM avatar", 0.1, asset_id="vrm-asset"
    )
    model.frame_rate = 30.0
    vrm_path = tmp_path / "avatar.vrm"
    vrm_path.write_bytes(b"VRM fixture")
    output = tmp_path / "vrm-output.mp4"
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake ffmpeg")
    captured = {"frames": []}

    def renderer(path, pose, fps):
        captured["frames"].append((Path(path), pose, fps))
        return _transparent_png_bytes()

    monkeypatch.setattr(
        timeline_video_render, "find_ffmpeg_executable", lambda *_args: str(executable)
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"rendered vrm mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    result = render_timeline_video(
        model,
        lambda _clip: vrm_path,
        output,
        ffmpeg_path=executable,
        vrm_frame_renderer=renderer,
    )

    assert result.character_clip_count == 1
    assert result.video_clip_count == 0
    assert len(captured["frames"]) == 3
    assert all(path == vrm_path for path, _pose, _fps in captured["frames"])
    assert all(fps == 30.0 for _path, _pose, fps in captured["frames"])
    assert all("bones" in pose and "expressions" in pose for _path, pose, _fps in captured["frames"])
    graph = captured["command"][captured["command"].index("-filter_complex") + 1]
    assert "format=rgba" in graph
    assert "overlay=" in graph
    assert output.read_bytes() == b"rendered vrm mp4"


def test_vrm_sequence_rejects_non_png_frame_output(tmp_path):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    clip = model.add_clip(
        overlay.id, "character", "VRM avatar", 0.1, asset_id="vrm-asset"
    )
    vrm_path = tmp_path / "avatar.vrm"
    vrm_path.write_bytes(b"VRM fixture")

    with pytest.raises(VideoRenderError, match="有効なPNG"):
        _render_vrm_sequence(
            clip,
            vrm_path,
            tmp_path / "frames",
            30.0,
            [],
            [],
            lambda *_args: b"not a png",
        )



def test_character_lane_assignments_separate_overlapping_clips_and_reuse_slots():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    first = model.add_clip(
        overlay.id, "character", "first", 2.0, start=0.0,
        script_line_id="__aural_builtin_2d_character__",
    )
    second = model.add_clip(
        overlay.id, "character", "second", 2.0, start=0.5,
        script_line_id="__aural_builtin_2d_character__",
    )
    third = model.add_clip(
        overlay.id, "character", "third", 0.75, start=1.0,
        script_line_id="__aural_builtin_2d_character__",
    )
    later = model.add_clip(
        overlay.id, "character", "later", 1.0, start=3.0,
        script_line_id="__aural_builtin_2d_character__",
    )

    layouts = _character_lane_assignments([first, second, third, later])

    assert layouts[first.id] == (0, 3)
    assert layouts[second.id] == (1, 3)
    assert layouts[third.id] == (2, 3)
    assert layouts[later.id] == (0, 1)


def test_motion_tracks_pair_with_character_tracks_in_track_order():
    model = TimelineModel.create_default()
    overlay_one = next(track for track in model.tracks if track.kind == "overlay")
    motion_one = next(track for track in model.tracks if track.kind == "motion")
    overlay_two = model.add_track("Overlay 2", "overlay")
    motion_two = model.add_track("Motion 2", "motion")
    character_one = model.add_clip(
        overlay_one.id, "character", "actor one", 1.0, asset_id="vrm-one"
    )
    character_two = model.add_clip(
        overlay_two.id, "character", "actor two", 1.0, asset_id="vrm-two"
    )
    motion_clip_one = model.add_clip(
        motion_one.id, "motion", "motion one", 1.0, asset_id="motion-one"
    )
    motion_clip_two = model.add_clip(
        motion_two.id, "motion", "motion two", 1.0, asset_id="motion-two"
    )
    frames_one = [{"time": 0.0, "marker": 1}]
    frames_two = [{"time": 0.0, "marker": 2}]
    entries = [(motion_clip_one, frames_one), (motion_clip_two, frames_two)]

    assert _motion_entries_for_character(model, character_one, entries) == [
        (motion_clip_one, frames_one)
    ]
    assert _motion_entries_for_character(model, character_two, entries) == [
        (motion_clip_two, frames_two)
    ]


def test_single_motion_track_remains_shared_by_multiple_character_tracks():
    model = TimelineModel.create_default()
    overlay_one = next(track for track in model.tracks if track.kind == "overlay")
    motion_track = next(track for track in model.tracks if track.kind == "motion")
    overlay_two = model.add_track("Overlay 2", "overlay")
    character_one = model.add_clip(
        overlay_one.id, "character", "actor one", 1.0,
        script_line_id="__aural_builtin_2d_character__",
    )
    character_two = model.add_clip(
        overlay_two.id, "character", "actor two", 1.0,
        script_line_id="__aural_builtin_2d_character__",
    )
    motion_clip = model.add_clip(
        motion_track.id, "motion", "shared motion", 1.0, asset_id="motion-shared"
    )
    entries = [(motion_clip, [{"time": 0.0}])]

    assert _motion_entries_for_character(model, character_one, entries) == entries
    assert _motion_entries_for_character(model, character_two, entries) == entries


def test_multiple_vrm_characters_render_side_by_side_with_independent_motion(
    tmp_path, monkeypatch
):
    import json

    model = TimelineModel.create_default()
    overlay_one = next(track for track in model.tracks if track.kind == "overlay")
    motion_one = next(track for track in model.tracks if track.kind == "motion")
    overlay_two = model.add_track("Overlay 2", "overlay")
    motion_two = model.add_track("Motion 2", "motion")
    character_one = model.add_clip(
        overlay_one.id, "character", "actor one", 0.1, asset_id="vrm-one"
    )
    character_two = model.add_clip(
        overlay_two.id, "character", "actor two", 0.1, asset_id="vrm-two"
    )
    motion_clip_one = model.add_clip(
        motion_one.id, "motion", "motion one", 0.1, asset_id="motion-one"
    )
    motion_clip_two = model.add_clip(
        motion_two.id, "motion", "motion two", 0.1, asset_id="motion-two"
    )

    vrm_one = tmp_path / "actor-one.vrm"
    vrm_two = tmp_path / "actor-two.vrm"
    motion_path_one = tmp_path / "actor-one.motion.json"
    motion_path_two = tmp_path / "actor-two.motion.json"
    vrm_one.write_bytes(b"VRM fixture one")
    vrm_two.write_bytes(b"VRM fixture two")
    motion_path_one.write_text(
        json.dumps({
            "format": "aural-motion",
            "version": 1,
            "keyframes": [
                {"time": 0.0, "head": {"tilt_deg": 0.0}},
                {"time": 0.1, "head": {"tilt_deg": 20.0}},
            ],
        }),
        encoding="utf-8",
    )
    motion_path_two.write_text(
        json.dumps({
            "format": "aural-motion",
            "version": 1,
            "keyframes": [
                {"time": 0.0, "head": {"tilt_deg": 0.0}},
                {"time": 0.1, "head": {"tilt_deg": 60.0}},
            ],
        }),
        encoding="utf-8",
    )

    output = tmp_path / "two-actors.mp4"
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake ffmpeg")
    assets = {
        character_one.id: vrm_one,
        character_two.id: vrm_two,
        motion_clip_one.id: motion_path_one,
        motion_clip_two.id: motion_path_two,
    }
    captured = {"frames": []}

    def frame_renderer(path, pose, fps):
        captured["frames"].append((Path(path), pose, fps))
        return _transparent_png_bytes()

    monkeypatch.setattr(
        timeline_video_render, "find_ffmpeg_executable", lambda *_args: str(executable)
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"two actor mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    result = render_timeline_video(
        model,
        lambda clip: assets[clip.id],
        output,
        ffmpeg_path=executable,
        vrm_frame_renderer=frame_renderer,
    )

    assert result.character_clip_count == 2
    assert result.motion_clip_count == 2
    assert len(captured["frames"]) == 6
    last_poses = {}
    for path, pose, _fps in captured["frames"]:
        last_poses[path] = pose
    left_rotation = last_poses[vrm_one]["bones"]["head"]["rotation"]
    right_rotation = last_poses[vrm_two]["bones"]["head"]["rotation"]
    assert left_rotation != right_rotation

    graph = captured["command"][captured["command"].index("-filter_complex") + 1]
    assert "overlay=x=(W-w)*0.333333333" in graph
    assert "overlay=x=(W-w)*0.666666667" in graph
    assert graph.count("format=rgba") >= 3
    assert output.read_bytes() == b"two actor mp4"



def test_character_stage_transform_controls_position_scale_and_rotation(
    tmp_path, monkeypatch
):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    actor = model.add_clip(
        overlay.id, "character", "positioned actor", 0.1, asset_id="actor.vrm"
    )
    model.set_character_transform(
        actor.id, position_x=0.25, position_y=0.6, scale=1.5, rotation_deg=30.0
    )
    vrm_path = tmp_path / "actor.vrm"
    vrm_path.write_bytes(b"VRM fixture")
    output = tmp_path / "positioned.mp4"
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake ffmpeg")

    monkeypatch.setattr(
        timeline_video_render, "find_ffmpeg_executable", lambda *_args: str(executable)
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"positioned mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timeline_video_render.subprocess, "run", fake_run)
    render_timeline_video(
        model,
        lambda _clip: vrm_path,
        output,
        still_image_canvas=(1280, 720),
        ffmpeg_path=executable,
        vrm_frame_renderer=lambda *_args: _transparent_png_bytes(),
    )

    graph = captured["command"][captured["command"].index("-filter_complex") + 1]
    assert "scale=-1:432" in graph
    assert "rotate=30*PI/180:ow=rotw(iw):oh=roth(ih):c=none" in graph
    assert "overlay=x=(W-w)*0.25:y=(H-h)*0.6" in graph
    assert output.read_bytes() == b"positioned mp4"


def test_motion_clips_for_character_uses_matching_track_when_multiple_tracks_exist():
    from timeline_video_render import motion_clips_for_character

    model = TimelineModel.create_default()
    character_track_a = next(track for track in model.tracks if track.kind == "overlay")
    motion_track_a = next(track for track in model.tracks if track.kind == "motion")
    character_track_b = model.add_track("Overlay 2", "overlay")
    motion_track_b = model.add_track("Motion 2", "motion")
    character_a = model.add_clip(
        character_track_a.id, "character", "actor A", 5.0, asset_id="actor-a.vrm"
    )
    character_b = model.add_clip(
        character_track_b.id, "character", "actor B", 5.0, asset_id="actor-b.vrm"
    )
    motion_a = model.add_clip(
        motion_track_a.id, "motion", "take A", 5.0, asset_id="motion-a"
    )
    motion_b = model.add_clip(
        motion_track_b.id, "motion", "take B", 5.0, asset_id="motion-b"
    )

    assert motion_clips_for_character(model, character_a, [motion_a, motion_b]) == [motion_a]
    assert motion_clips_for_character(model, character_b, [motion_a, motion_b]) == [motion_b]


def test_motion_clips_for_character_keeps_single_motion_track_shared():
    from timeline_video_render import motion_clips_for_character

    model = TimelineModel.create_default()
    character_track_a = next(track for track in model.tracks if track.kind == "overlay")
    character_track_b = model.add_track("Overlay 2", "overlay")
    motion_track = next(track for track in model.tracks if track.kind == "motion")
    character_a = model.add_clip(
        character_track_a.id, "character", "actor A", 5.0, asset_id="actor-a.vrm"
    )
    character_b = model.add_clip(
        character_track_b.id, "character", "actor B", 5.0, asset_id="actor-b.vrm"
    )
    shared_motion = model.add_clip(
        motion_track.id, "motion", "shared take", 5.0, asset_id="shared-motion"
    )

    assert motion_clips_for_character(model, character_a, [shared_motion]) == [shared_motion]
    assert motion_clips_for_character(model, character_b, [shared_motion]) == [shared_motion]

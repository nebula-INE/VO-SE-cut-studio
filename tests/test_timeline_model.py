from __future__ import annotations

import pytest

from timeline_model import TimelineError, TimelineModel


def _model_with_clip():
    model = TimelineModel.create_default()
    video_track = next(track for track in model.tracks if track.kind == "video")
    clip = model.add_clip(
        video_track.id,
        "video",
        "scene.mov",
        12.0,
        start=2.0,
        source_in=1.5,
        asset_id="asset-video",
    )
    return model, video_track, clip


def test_timeline_round_trip_preserves_tracks_clips_and_frame_rate():
    model, _, clip = _model_with_clip()
    model.frame_rate = 29.97

    restored = TimelineModel.from_dict(model.to_dict())

    assert restored.frame_rate == 29.97
    assert len(restored.tracks) == 4
    restored_clip = restored.get_clip(clip.id)
    assert restored_clip is not None
    assert restored_clip.start == 2.0
    assert restored_clip.source_in == 1.5
    assert restored_clip.duration == 12.0
    assert restored_clip.asset_id == "asset-video"


def test_move_clip_snaps_to_grid_and_can_move_between_compatible_tracks():
    model, _, clip = _model_with_clip()
    second_video_track = model.add_track("Video 2", "video")

    moved = model.move_clip(clip.id, 4.24, track_id=second_video_track.id)

    assert moved.start == 4.2
    assert moved.track_id == second_video_track.id


def test_clip_cannot_move_to_incompatible_track():
    model, _, clip = _model_with_clip()
    audio_track = next(track for track in model.tracks if track.kind == "audio")

    with pytest.raises(TimelineError, match="different kind"):
        model.move_clip(clip.id, 5.0, track_id=audio_track.id)


def test_trim_changes_source_in_and_duration_without_changing_timeline_start():
    model, _, clip = _model_with_clip()

    trimmed = model.trim_clip(clip.id, source_in=3.0, duration=6.5)

    assert trimmed.start == 2.0
    assert trimmed.source_in == 3.0
    assert trimmed.duration == 6.5
    assert trimmed.source_out == 9.5


def test_split_creates_two_non_overlapping_source_contiguous_clips():
    model, _, clip = _model_with_clip()

    left, right = model.split_clip(clip.id, 8.0)

    assert left.id == clip.id
    assert left.start == 2.0
    assert left.duration == 6.0
    assert right.start == 8.0
    assert right.duration == 6.0
    assert right.source_in == 7.5
    assert left.source_out == right.source_in
    assert len(model.clips) == 2


def test_split_too_close_to_edges_is_rejected():
    model, _, clip = _model_with_clip()

    with pytest.raises(TimelineError, match="Split point"):
        model.split_clip(clip.id, clip.start + 0.01)

    with pytest.raises(TimelineError, match="Split point"):
        model.split_clip(clip.id, clip.end - 0.01)


def test_remove_track_requires_explicit_clip_removal():
    model, track, clip = _model_with_clip()

    with pytest.raises(TimelineError, match="still contains clips"):
        model.remove_track(track.id)

    assert model.get_clip(clip.id) is not None
    assert model.remove_track(track.id, remove_clips=True)
    assert model.get_clip(clip.id) is None


def test_clip_timing_and_track_references_are_validated_on_load():
    model, _, _ = _model_with_clip()
    payload = model.to_dict()
    payload["clips"][0]["start"] = -1

    with pytest.raises(TimelineError, match="cannot be negative"):
        TimelineModel.from_dict(payload)

    payload = model.to_dict()
    payload["clips"][0]["track_id"] = "missing-track"
    with pytest.raises(TimelineError, match="unknown track"):
        TimelineModel.from_dict(payload)


def test_timeline_duration_is_maximum_clip_end_not_sum_of_clip_durations():
    model, track, _ = _model_with_clip()
    model.add_clip(track.id, "video", "later.mov", 3.0, start=20.0, asset_id="later")

    assert model.duration_sec == 23.0


def test_project_document_persists_timeline_payload(tmp_path):
    from project_model import ProjectDocument

    project = ProjectDocument.create("Timeline Project")
    timeline = TimelineModel.create_default()
    track = next(item for item in timeline.tracks if item.kind == "audio")
    clip = timeline.add_clip(
        track.id,
        "audio",
        "music.wav",
        18.0,
        start=3.0,
        asset_id="asset-audio",
    )
    project.timeline = timeline.to_dict()

    saved_path = project.save(tmp_path / "timeline.auralproj")
    restored_project = ProjectDocument.load(saved_path)
    restored_timeline = TimelineModel.from_dict(restored_project.timeline)

    restored_clip = restored_timeline.get_clip(clip.id)
    assert restored_clip is not None
    assert restored_clip.start == 3.0
    assert restored_clip.duration == 18.0
    assert restored_clip.asset_id == "asset-audio"


def test_clip_converts_between_timeline_and_source_positions_with_clamping():
    model, _, clip = _model_with_clip()

    assert clip.source_time_at(2.0) == 1.5
    assert clip.source_time_at(5.0) == 4.5
    assert clip.source_time_at(0.0) == 1.5
    assert clip.source_time_at(99.0) == 13.5
    assert clip.timeline_time_at(1.5) == 2.0
    assert clip.timeline_time_at(4.5) == 5.0
    assert clip.timeline_time_at(0.0) == 2.0
    assert clip.timeline_time_at(999.0) == clip.end


def test_next_clip_after_returns_next_enabled_clip_on_same_track_across_gap():
    model, track, current = _model_with_clip()
    disabled = model.add_clip(
        track.id, "video", "disabled.mov", 2.0, start=current.end, asset_id="disabled"
    )
    disabled.enabled = False
    overlapping = model.add_clip(
        track.id, "video", "overlap.mov", 2.0, start=current.end - 0.5, asset_id="overlap"
    )
    later_track = model.add_track("Video 2", "video")
    model.add_clip(
        later_track.id, "video", "other-track.mov", 2.0, start=current.end, asset_id="other"
    )
    next_clip = model.add_clip(
        track.id, "video", "next.mov", 2.0, start=current.end + 1.25, asset_id="next"
    )

    assert model.next_clip_after(current.id) is next_clip
    assert model.next_clip_after(disabled.id) is None
    assert model.next_clip_after(overlapping.id) is None


def test_next_clip_after_returns_none_for_unknown_or_final_clip():
    model, _, clip = _model_with_clip()

    assert model.next_clip_after("missing") is None
    assert model.next_clip_after(clip.id) is None


def test_upsert_voice_clip_preserves_user_moved_and_trimmed_existing_clip():
    model = TimelineModel.create_default()
    voice_track = next(track for track in model.tracks if track.kind == "audio")
    alternate_track = model.add_track("Dialogue", "audio")
    existing = model.add_clip(
        voice_track.id,
        "voice",
        "Narrator: original",
        4.0,
        start=12.5,
        source_in=0.75,
        asset_id="old-voice",
        script_line_id="line-voice-1",
    )
    model.move_clip(existing.id, 12.5, track_id=alternate_track.id, snap_seconds=None)
    model.trim_clip(existing.id, source_in=1.25, duration=2.5)

    restored, changed = model.upsert_voice_clip(
        voice_track.id,
        "Narrator: regenerated",
        3.0,
        start=0.0,
        asset_id="new-voice",
        script_line_id="line-voice-1",
    )

    assert restored is existing
    assert changed
    assert existing.track_id == alternate_track.id
    assert existing.name == "Narrator: original"
    assert existing.start == 12.5
    assert existing.duration == 2.5
    assert existing.source_in == 1.25
    assert existing.asset_id == "new-voice"
    assert len([clip for clip in model.clips if clip.script_line_id == "line-voice-1"]) == 1


def test_upsert_voice_clip_creates_missing_line_at_script_position():
    model = TimelineModel.create_default()
    voice_track = next(track for track in model.tracks if track.kind == "audio")

    created, changed = model.upsert_voice_clip(
        voice_track.id,
        "Narrator: first line",
        1.75,
        start=3.25,
        asset_id="voice-asset",
        script_line_id="line-new",
    )

    assert changed
    assert created.kind == "voice"
    assert created.track_id == voice_track.id
    assert created.start == 3.25
    assert created.duration == 1.75
    assert created.source_in == 0.0
    assert created.asset_id == "voice-asset"
    assert created.script_line_id == "line-new"



def test_legacy_audio_clip_payload_defaults_to_full_volume_without_fades():
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    clip = model.add_clip(track.id, "audio", "legacy.wav", 2.0, asset_id="legacy")
    payload = model.to_dict()
    for key in ("volume", "fade_in", "fade_out"):
        payload["clips"][0].pop(key)

    restored = TimelineModel.from_dict(payload).get_clip(clip.id)

    assert restored is not None
    assert restored.volume == 1.0
    assert restored.fade_in == 0.0
    assert restored.fade_out == 0.0


def test_audio_clip_volume_and_fades_round_trip_and_can_be_updated():
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    clip = model.add_clip(
        track.id,
        "voice",
        "narration.wav",
        5.0,
        asset_id="voice",
        volume=0.65,
        fade_in=0.4,
        fade_out=1.2,
    )

    restored = TimelineModel.from_dict(model.to_dict()).get_clip(clip.id)
    assert restored is not None
    assert restored.volume == 0.65
    assert restored.fade_in == 0.4
    assert restored.fade_out == 1.2

    model.set_audio_properties(clip.id, volume=0.3, fade_in=0.8, fade_out=0.5)
    assert clip.volume == 0.3
    assert clip.fade_in == 0.8
    assert clip.fade_out == 0.5


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"volume": -0.1}, "between 0 and 1"),
        ({"volume": 1.1}, "between 0 and 1"),
        ({"fade_in": -0.1}, "cannot be negative"),
        ({"fade_out": 2.1}, "cannot exceed clip duration"),
    ],
)
def test_audio_clip_properties_reject_invalid_values(settings, message):
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    clip = model.add_clip(track.id, "audio", "music.wav", 2.0, asset_id="music")

    with pytest.raises(TimelineError, match=message):
        model.set_audio_properties(clip.id, **settings)


def test_trimming_audio_clip_clamps_fades_to_new_clip_duration():
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    clip = model.add_clip(
        track.id, "audio", "music.wav", 5.0, asset_id="music",
        fade_in=4.0, fade_out=3.0,
    )

    model.trim_clip(clip.id, duration=1.5)

    assert clip.duration == 1.5
    assert clip.fade_in == 1.5
    assert clip.fade_out == 1.5


def test_split_audio_clip_keeps_fades_on_outer_edges_and_volume_on_both_parts():
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    clip = model.add_clip(
        track.id, "audio", "music.wav", 8.0, asset_id="music",
        volume=0.7, fade_in=1.0, fade_out=2.0,
    )

    left, right = model.split_clip(clip.id, 4.0)

    assert left.fade_in == 1.0
    assert left.fade_out == 0.0
    assert right.fade_in == 0.0
    assert right.fade_out == 2.0
    assert left.volume == right.volume == 0.7

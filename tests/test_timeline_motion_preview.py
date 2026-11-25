import pytest

from timeline_model import TimelineModel
from timeline_motion_preview import (
    TimelineMotionPreviewError,
    timeline_motion_state_at,
)


def _timeline_with_motion(*, character_visible=True, motion_visible=True):
    model = TimelineModel.create_default()
    character_track = next(track for track in model.tracks if track.kind == "overlay")
    motion_track = next(track for track in model.tracks if track.kind == "motion")
    character_track.visible = character_visible
    motion_track.visible = motion_visible
    model.add_clip(
        character_track.id,
        "character",
        "2D Character",
        4.0,
        start=1.0,
    )
    motion_clip = model.add_clip(
        motion_track.id,
        "motion",
        "Take",
        2.0,
        start=1.5,
        source_in=0.25,
        asset_id="motion-asset",
    )
    return model, motion_clip


def _keyframes():
    return [
        {"time": 0.0, "head": {"x": 0.0, "tilt_deg": 0.0}},
        {"time": 1.0, "head": {"x": 1.0, "tilt_deg": 20.0}},
    ]


def test_timeline_motion_preview_uses_clip_source_in_and_interpolation():
    model, motion_clip = _timeline_with_motion()
    loaded = []

    def load(clip):
        loaded.append(clip.id)
        return _keyframes()

    state = timeline_motion_state_at(model, 2.0, load)

    assert loaded == [motion_clip.id]
    assert state["head"]["x"] == pytest.approx(0.75)
    assert state["head"]["tilt_deg"] == pytest.approx(15.0)


@pytest.mark.parametrize(
    ("character_visible", "motion_visible"),
    [(False, True), (True, False)],
)
def test_hidden_character_or_motion_track_does_not_drive_preview(
    character_visible,
    motion_visible,
):
    model, _ = _timeline_with_motion(
        character_visible=character_visible,
        motion_visible=motion_visible,
    )

    state = timeline_motion_state_at(
        model,
        2.0,
        lambda _clip: pytest.fail("hidden clips must not load motion assets"),
    )

    assert state is None


def test_muted_and_disabled_motion_clips_do_not_drive_preview():
    model, motion_clip = _timeline_with_motion()
    motion_track = model.get_track(motion_clip.track_id)
    motion_track.muted = True
    assert timeline_motion_state_at(model, 2.0, lambda _clip: _keyframes()) is None

    motion_track.muted = False
    motion_clip.enabled = False
    assert timeline_motion_state_at(model, 2.0, lambda _clip: _keyframes()) is None


def test_motion_preview_requires_an_active_character_clip():
    model, _ = _timeline_with_motion()
    assert timeline_motion_state_at(model, 0.5, lambda _clip: _keyframes()) is None
    assert timeline_motion_state_at(model, 5.0, lambda _clip: _keyframes()) is None


def test_overlapping_motion_clips_are_rejected():
    model, motion_clip = _timeline_with_motion()
    model.add_clip(
        motion_clip.track_id,
        "motion",
        "Overlapping take",
        1.0,
        start=2.0,
        asset_id="another-motion",
    )

    with pytest.raises(TimelineMotionPreviewError, match="複数"):
        timeline_motion_state_at(model, 2.25, lambda _clip: _keyframes())


def test_missing_motion_asset_reference_is_reported():
    model, motion_clip = _timeline_with_motion()
    motion_clip.asset_id = None

    with pytest.raises(TimelineMotionPreviewError, match="素材がありません"):
        timeline_motion_state_at(model, 2.0, lambda _clip: _keyframes())


@pytest.mark.parametrize("position", [-1.0, float("nan"), float("inf")])
def test_invalid_timeline_positions_are_rejected(position):
    model, _ = _timeline_with_motion()
    with pytest.raises(TimelineMotionPreviewError, match="finite"):
        timeline_motion_state_at(model, position, lambda _clip: _keyframes())

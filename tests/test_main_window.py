from __future__ import annotations

from timeline_model import TimelineModel
from timeline_track_selection import select_timeline_asset_track


def test_asset_track_selection_skips_dialog_when_only_one_track_is_available():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")

    def unexpected_dialog(*args, **kwargs):
        raise AssertionError("A track-selection dialog is unnecessary for one candidate")

    selected = select_timeline_asset_track(
        None, model, [overlay], "character", dialog_get_item=unexpected_dialog
    )

    assert selected is overlay


def test_asset_track_selection_shows_track_names_and_clip_counts():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    second = model.add_track("Character B", "overlay")
    model.add_clip(second.id, "character", "actor-b.vrm", 4.0, asset_id="actor-b")
    captured = {}

    def choose_track(parent, title, prompt, labels, current, editable):
        captured.update(
            parent=parent,
            title=title,
            prompt=prompt,
            labels=labels,
            current=current,
            editable=editable,
        )
        return labels[1], True

    selected = select_timeline_asset_track(
        "parent", model, [overlay, second], "character",
        dialog_get_item=choose_track,
    )

    assert selected is second
    assert captured["parent"] == "parent"
    assert captured["title"] == "配置先トラック"
    assert captured["prompt"] == "キャラクタークリップの配置先:"
    assert captured["labels"] == [
        f"1. {overlay.name} (0 件)",
        "2. Character B (1 件)",
    ]
    assert captured["current"] == 0
    assert captured["editable"] is False


def test_asset_track_selection_returns_none_when_user_cancels():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    second = model.add_track("Character B", "overlay")
    before = model.to_dict()

    selected = select_timeline_asset_track(
        None, model, [overlay, second], "character",
        dialog_get_item=lambda *args, **kwargs: ("", False),
    )

    assert selected is None
    assert model.to_dict() == before

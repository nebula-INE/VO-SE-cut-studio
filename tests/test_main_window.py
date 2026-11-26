from __future__ import annotations

from types import SimpleNamespace

import main_window
from main_window import MainWindow
from timeline_model import TimelineModel


def _window_stub(model: TimelineModel):
    return SimpleNamespace(timeline_panel=SimpleNamespace(model=model))


def test_asset_track_selection_skips_dialog_when_only_one_track_is_available(monkeypatch):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    window = _window_stub(model)

    def unexpected_dialog(*args, **kwargs):
        raise AssertionError("A track-selection dialog is unnecessary for one candidate")

    monkeypatch.setattr(main_window.QInputDialog, "getItem", unexpected_dialog)

    selected = MainWindow._select_timeline_asset_track(window, [overlay], "character")

    assert selected is overlay


def test_asset_track_selection_shows_track_names_and_clip_counts(monkeypatch):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    second = model.add_track("Character B", "overlay")
    model.add_clip(second.id, "character", "actor-b.vrm", 4.0, asset_id="actor-b")
    window = _window_stub(model)
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

    monkeypatch.setattr(main_window.QInputDialog, "getItem", choose_track)

    selected = MainWindow._select_timeline_asset_track(
        window, [overlay, second], "character"
    )

    assert selected is second
    assert captured["parent"] is window
    assert captured["title"] == "配置先トラック"
    assert captured["prompt"] == "キャラクタークリップの配置先:"
    assert captured["labels"] == [
        f"1. {overlay.name} (0 件)",
        "2. Character B (1 件)",
    ]
    assert captured["current"] == 0
    assert captured["editable"] is False


def test_asset_track_selection_returns_none_when_user_cancels(monkeypatch):
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    second = model.add_track("Character B", "overlay")
    window = _window_stub(model)

    monkeypatch.setattr(
        main_window.QInputDialog,
        "getItem",
        lambda *args, **kwargs: ("", False),
    )

    selected = MainWindow._select_timeline_asset_track(
        window, [overlay, second], "character"
    )

    assert selected is None
    assert len(model.tracks) == 5
    assert not model.clips

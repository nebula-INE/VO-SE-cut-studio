from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from timeline_editor import TimelineCanvas, TimelineEditorWidget
from timeline_model import TimelineModel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _model_with_locked_clip():
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "video")
    track.locked = True
    clip = model.add_clip(
        track.id, "video", "locked.mov", 5.0, asset_id="video-asset"
    )
    return model, track, clip


def test_locked_track_clip_cannot_be_deleted_with_keyboard(qapp):
    model, _, clip = _model_with_locked_clip()
    canvas = TimelineCanvas(model)
    canvas._set_selected_clip(clip.id)

    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Delete,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.keyPressEvent(event)

    assert model.get_clip(clip.id) is not None


def test_locked_track_disables_split_and_delete_actions(qapp):
    model, _, clip = _model_with_locked_clip()
    editor = TimelineEditorWidget(model)
    editor.canvas._set_selected_clip(clip.id)
    editor._on_selection_changed(clip.id)

    assert not editor.split_button.isEnabled()
    assert not editor.delete_button.isEnabled()


def test_locked_track_cannot_be_split_or_deleted_from_toolbar(qapp):
    model, _, clip = _model_with_locked_clip()
    editor = TimelineEditorWidget(model)
    editor.canvas._set_selected_clip(clip.id)
    editor.canvas.set_playhead(2.5)

    editor.split_selected_at_playhead()
    editor.delete_selected()

    assert len(model.clips) == 1
    assert model.get_clip(clip.id) is not None

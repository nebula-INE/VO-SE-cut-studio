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
    # Release every top-level test widget while QApplication is still alive.
    # This avoids PySide6's shared-QObject teardown abort on macOS runners.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


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


def test_timeline_transport_buttons_emit_start_pause_and_stop_requests(qapp):
    editor = TimelineEditorWidget(TimelineModel.create_default())
    requests = []
    editor.playback_toggle_requested.connect(lambda: requests.append("toggle"))
    editor.playback_stop_requested.connect(lambda: requests.append("stop"))

    editor.playback_button.click()
    editor.set_playback_state(True)
    assert editor.playback_button.text() == "Ⅱ 一時停止"
    editor.playback_button.click()
    editor.stop_playback_button.click()

    assert requests == ["toggle", "toggle", "stop"]
    editor.close()
    editor.deleteLater()



def test_audio_clip_controls_edit_volume_and_fades(qapp):
    model = TimelineModel.create_default()
    track = next(item for item in model.tracks if item.kind == "audio")
    clip = model.add_clip(
        track.id, "voice", "narration.wav", 4.0, asset_id="voice",
        volume=0.8, fade_in=0.5, fade_out=1.0,
    )
    editor = TimelineEditorWidget(model)

    editor.canvas._set_selected_clip(clip.id)
    assert editor.volume_spinbox.isEnabled()
    assert editor.fade_in_spinbox.value() == 0.5
    assert editor.fade_out_spinbox.value() == 1.0

    editor.volume_spinbox.setValue(55.0)
    editor.fade_in_spinbox.setValue(0.75)
    editor.fade_out_spinbox.setValue(1.25)

    assert clip.volume == 0.55
    assert clip.fade_in == 0.75
    assert clip.fade_out == 1.25

    editor.close()
    editor.deleteLater()


def test_audio_clip_controls_are_disabled_for_video_and_locked_audio(qapp):
    model = TimelineModel.create_default()
    video_track = next(item for item in model.tracks if item.kind == "video")
    audio_track = next(item for item in model.tracks if item.kind == "audio")
    video = model.add_clip(video_track.id, "video", "scene.mp4", 2.0, asset_id="video")
    audio = model.add_clip(audio_track.id, "audio", "music.wav", 2.0, asset_id="music")
    editor = TimelineEditorWidget(model)

    editor.canvas._set_selected_clip(video.id)
    assert not editor.volume_spinbox.isEnabled()

    audio_track.locked = True
    editor.canvas._set_selected_clip(audio.id)
    assert not editor.volume_spinbox.isEnabled()

    editor.close()
    editor.deleteLater()



def test_character_transform_controls_edit_selected_character_clip(qapp):
    model = TimelineModel.create_default()
    overlay = next(item for item in model.tracks if item.kind == "overlay")
    clip = model.add_clip(
        overlay.id, "character", "actor", 2.0, asset_id="actor.vrm"
    )
    editor = TimelineEditorWidget(model)
    editor.canvas._set_selected_clip(clip.id)

    assert editor.auto_character_position_checkbox.isEnabled()
    assert editor.auto_character_position_checkbox.isChecked()
    assert not editor.position_x_spinbox.isEnabled()

    editor.auto_character_position_checkbox.setChecked(False)
    editor.position_x_spinbox.setValue(25.0)
    editor.position_y_spinbox.setValue(70.0)
    editor.character_scale_spinbox.setValue(135.0)
    editor.character_rotation_spinbox.setValue(-22.5)

    assert clip.position_x == pytest.approx(0.25)
    assert clip.position_y == pytest.approx(0.70)
    assert clip.scale == pytest.approx(1.35)
    assert clip.rotation_deg == pytest.approx(-22.5)

    editor.close()
    editor.deleteLater()


def test_character_transform_controls_are_disabled_for_non_character_or_locked_clip(qapp):
    model = TimelineModel.create_default()
    video_track = next(item for item in model.tracks if item.kind == "video")
    overlay = next(item for item in model.tracks if item.kind == "overlay")
    video = model.add_clip(video_track.id, "video", "scene", 2.0, asset_id="scene.mp4")
    actor = model.add_clip(overlay.id, "character", "actor", 2.0, asset_id="actor.vrm")
    editor = TimelineEditorWidget(model)

    editor.canvas._set_selected_clip(video.id)
    assert not editor.auto_character_position_checkbox.isEnabled()

    overlay.locked = True
    editor.canvas._set_selected_clip(actor.id)
    assert not editor.auto_character_position_checkbox.isEnabled()

    editor.close()
    editor.deleteLater()

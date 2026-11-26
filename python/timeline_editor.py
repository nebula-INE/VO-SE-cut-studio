"""Qt timeline editor with non-destructive move, trim, split and delete operations."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QRectF, Signal, QSignalBlocker
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from timeline_model import (
    MIN_CLIP_DURATION_SEC,
    TimelineClip,
    TimelineError,
    TimelineModel,
)


class TimelineCanvas(QWidget):
    """Scrollable multi-track canvas. Drag clip bodies to move and edges to trim."""

    changed = Signal()
    playhead_changed = Signal(float)
    selection_changed = Signal(str)

    LABEL_WIDTH = 132
    RULER_HEIGHT = 30
    ROW_HEIGHT = 70
    CLIP_INSET = 8
    CLIP_HEIGHT = 54
    HANDLE_WIDTH = 7

    _CLIP_COLORS = {
        "video": QColor("#315d86"),
        "audio": QColor("#39765f"),
        "voice": QColor("#39765f"),
        "image": QColor("#80613d"),
        "text": QColor("#6b568e"),
        "character": QColor("#6b568e"),
        "motion": QColor("#7a5b77"),
        "other": QColor("#626b78"),
    }

    def __init__(self, model: TimelineModel) -> None:
        super().__init__()
        self.model = model
        self.pixels_per_second = 72
        self.playhead_seconds = 0.0
        self.selected_clip_id: str | None = None
        self._drag_mode: str | None = None
        self._drag_clip_id: str | None = None
        self._drag_origin_x = 0.0
        self._original_start = 0.0
        self._original_duration = 0.0
        self._original_source_in = 0.0
        self._drag_changed = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(self.sizeHint())

    def sizeHint(self) -> QSize:
        visible_duration = max(30.0, self.model.duration_sec + 5.0)
        width = self.LABEL_WIDTH + int(visible_duration * self.pixels_per_second)
        width = min(max(width, 1100), 180_000)
        height = self.RULER_HEIGHT + max(1, len(self.model.tracks)) * self.ROW_HEIGHT
        return QSize(width, height)

    def refresh(self) -> None:
        self.setMinimumSize(self.sizeHint())
        self.updateGeometry()
        self.update()

    def set_model(self, model: TimelineModel) -> None:
        self.model = model
        self.selected_clip_id = None
        self.playhead_seconds = 0.0
        self._drag_mode = None
        self._drag_clip_id = None
        self.refresh()

    def set_playhead(self, seconds: float) -> None:
        self.playhead_seconds = max(0.0, float(seconds))
        self.update()

    def _track_at_y(self, y: float):
        if y < self.RULER_HEIGHT:
            return None
        index = int((y - self.RULER_HEIGHT) // self.ROW_HEIGHT)
        if 0 <= index < len(self.model.tracks):
            return self.model.tracks[index]
        return None

    def _clip_rect(self, clip: TimelineClip) -> QRectF | None:
        try:
            row = next(i for i, track in enumerate(self.model.tracks) if track.id == clip.track_id)
        except StopIteration:
            return None
        x = self.LABEL_WIDTH + clip.start * self.pixels_per_second
        width = max(10.0, clip.duration * self.pixels_per_second)
        y = self.RULER_HEIGHT + row * self.ROW_HEIGHT + self.CLIP_INSET
        return QRectF(x, y, width, self.CLIP_HEIGHT)

    def _clip_at(self, x: float, y: float):
        for clip in reversed(self.model.clips):
            rect = self._clip_rect(clip)
            if rect is not None and rect.contains(x, y):
                return clip, rect
        return None, None

    @staticmethod
    def _snap_time(seconds: float) -> float:
        return round(seconds / 0.1) * 0.1

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#151a22"))

        # Ruler and track label header.
        painter.fillRect(0, 0, self.width(), self.RULER_HEIGHT, QColor("#222b37"))
        painter.fillRect(0, self.RULER_HEIGHT, self.LABEL_WIDTH, self.height(), QColor("#202833"))
        painter.setPen(QPen(QColor("#d6dee8")))
        painter.setFont(QFont("Sans Serif", 9, QFont.Weight.DemiBold))
        painter.drawText(12, 20, "TRACK / TIME")

        visible_end = max(0, (self.width() - self.LABEL_WIDTH) // self.pixels_per_second)
        for second in range(visible_end + 1):
            x = self.LABEL_WIDTH + second * self.pixels_per_second
            color = QColor("#667487") if second % 5 == 0 else QColor("#3a4655")
            painter.setPen(QPen(color, 1))
            painter.drawLine(x, self.RULER_HEIGHT - (10 if second % 5 == 0 else 5), x, self.height())
            if second % 5 == 0:
                painter.setPen(QPen(QColor("#d6dee8")))
                painter.setFont(QFont("Sans Serif", 8))
                painter.drawText(x + 4, 19, f"{second // 60:02d}:{second % 60:02d}")

        for index, track in enumerate(self.model.tracks):
            y = self.RULER_HEIGHT + index * self.ROW_HEIGHT
            row_color = QColor("#1a222d") if index % 2 == 0 else QColor("#18202a")
            painter.fillRect(self.LABEL_WIDTH, y, self.width() - self.LABEL_WIDTH, self.ROW_HEIGHT, row_color)
            painter.setPen(QPen(QColor("#364251")))
            painter.drawLine(0, y + self.ROW_HEIGHT - 1, self.width(), y + self.ROW_HEIGHT - 1)
            painter.setPen(QPen(QColor("#e2e8f0") if not track.locked else QColor("#7c8795")))
            painter.setFont(QFont("Sans Serif", 9, QFont.Weight.DemiBold))
            painter.drawText(10, y + 25, track.name)
            painter.setPen(QPen(QColor("#8996a7")))
            painter.setFont(QFont("Sans Serif", 8))
            state = "LOCKED" if track.locked else track.kind.upper()
            if track.muted:
                state += " · MUTED"
            painter.drawText(10, y + 43, state)

        for clip in self.model.clips:
            rect = self._clip_rect(clip)
            if rect is None or rect.right() < self.LABEL_WIDTH or rect.left() > self.width():
                continue
            color = self._CLIP_COLORS[clip.kind]
            painter.setBrush(color if clip.enabled else color.darker(250))
            painter.setPen(QPen(color.lighter(145), 1))
            painter.drawRoundedRect(rect, 5, 5)
            if clip.id == self.selected_clip_id:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#f5d76e"), 2))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
            painter.setPen(QPen(QColor("#ffffff")))
            painter.setFont(QFont("Sans Serif", 8, QFont.Weight.DemiBold))
            text_width = max(0, int(rect.width()) - 12)
            label = painter.fontMetrics().elidedText(
                clip.name, Qt.TextElideMode.ElideRight, text_width
            )
            painter.drawText(
                rect.adjusted(7, 4, -7, -4),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                label,
            )
            painter.setPen(QPen(color.lighter(175), 2))
            painter.drawLine(rect.left() + 4, rect.top() + 8, rect.left() + 4, rect.bottom() - 8)
            painter.drawLine(rect.right() - 4, rect.top() + 8, rect.right() - 4, rect.bottom() - 8)

        playhead_x = self.LABEL_WIDTH + self.playhead_seconds * self.pixels_per_second
        painter.setPen(QPen(QColor("#f3d35c"), 2))
        painter.drawLine(playhead_x, 0, playhead_x, self.height())
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        x = float(event.position().x())
        y = float(event.position().y())
        clip, rect = self._clip_at(x, y)
        if clip is None or rect is None:
            if x >= self.LABEL_WIDTH:
                self.playhead_seconds = max(0.0, (x - self.LABEL_WIDTH) / self.pixels_per_second)
                self.playhead_changed.emit(self.playhead_seconds)
                self.update()
            self._set_selected_clip(None)
            self._drag_mode = None
            return

        self._set_selected_clip(clip.id)
        track = self.model.get_track(clip.track_id)
        if track is None or track.locked:
            return
        self._drag_clip_id = clip.id
        self._drag_origin_x = x
        self._original_start = clip.start
        self._original_duration = clip.duration
        self._original_source_in = clip.source_in
        if x - rect.left() <= self.HANDLE_WIDTH:
            self._drag_mode = "trim_left"
        elif rect.right() - x <= self.HANDLE_WIDTH:
            self._drag_mode = "trim_right"
        else:
            self._drag_mode = "move"
        self._drag_changed = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag_mode is None or self._drag_clip_id is None:
            return
        clip = self.model.get_clip(self._drag_clip_id)
        if clip is None:
            return
        delta = self._snap_time((float(event.position().x()) - self._drag_origin_x) / self.pixels_per_second)
        try:
            if self._drag_mode == "move":
                target_track = self._track_at_y(float(event.position().y()))
                track_id = target_track.id if target_track is not None and not target_track.locked else clip.track_id
                self.model.move_clip(
                    clip.id,
                    max(0.0, self._original_start + delta),
                    track_id=track_id,
                    snap_seconds=0.1,
                )
            elif self._drag_mode == "trim_right":
                self.model.trim_clip(
                    clip.id,
                    duration=max(MIN_CLIP_DURATION_SEC, self._original_duration + delta),
                )
            elif self._drag_mode == "trim_left":
                minimum_delta = max(-self._original_source_in, -self._original_start)
                maximum_delta = self._original_duration - MIN_CLIP_DURATION_SEC
                adjusted_delta = min(max(delta, minimum_delta), maximum_delta)
                new_start = self._original_start + adjusted_delta
                new_source_in = self._original_source_in + adjusted_delta
                new_duration = self._original_duration - adjusted_delta
                clip.start = new_start
                self.model.trim_clip(
                    clip.id,
                    source_in=new_source_in,
                    duration=new_duration,
                )
        except TimelineError:
            return
        self._drag_changed = True
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_changed:
            self.changed.emit()
        self._drag_mode = None
        self._drag_clip_id = None
        self._drag_changed = False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            clip = self.model.get_clip(self.selected_clip_id or "")
            track = self.model.get_track(clip.track_id) if clip is not None else None
            if clip is not None and track is not None and not track.locked:
                if self.model.remove_clip(clip.id):
                    self._set_selected_clip(None)
                    self.refresh()
                    self.changed.emit()
                    return
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            clip = self.model.get_clip(self.selected_clip_id or "")
            if clip is not None:
                direction = -1.0 if event.key() == Qt.Key.Key_Left else 1.0
                track = self.model.get_track(clip.track_id)
                if track is not None and not track.locked:
                    try:
                        self.model.move_clip(clip.id, clip.start + direction / self.model.frame_rate, snap_seconds=0.0)
                        self.refresh()
                        self.changed.emit()
                    except TimelineError:
                        pass
                return
        super().keyPressEvent(event)

    def _set_selected_clip(self, clip_id: str | None) -> None:
        if self.selected_clip_id == clip_id:
            return
        self.selected_clip_id = clip_id
        self.selection_changed.emit(clip_id or "")
        self.update()


class TimelineEditorWidget(QWidget):
    """Toolbar + scrollable timeline canvas used as a MainWindow dock."""

    changed = Signal()
    playhead_changed = Signal(float)
    add_clip_requested = Signal()
    add_track_requested = Signal()
    playback_toggle_requested = Signal()
    playback_stop_requested = Signal()

    def __init__(self, model: TimelineModel | None = None) -> None:
        super().__init__()
        self.model = model or TimelineModel.create_default()

        self.playback_button = QPushButton("▶ 再生")
        self.stop_playback_button = QPushButton("■ 停止")
        self.add_clip_button = QPushButton("＋ クリップ")
        self.add_track_button = QPushButton("＋ トラック")
        self.split_button = QPushButton("分割")
        self.delete_button = QPushButton("削除")
        self.zoom_out_button = QPushButton("−")
        self.zoom_in_button = QPushButton("＋")
        self.selection_label = QLabel("クリップ未選択")
        self.status_label = QLabel("ドラッグ: 移動／左右端: トリム")

        self.volume_spinbox = QDoubleSpinBox()
        self.volume_spinbox.setRange(0.0, 100.0)
        self.volume_spinbox.setDecimals(0)
        self.volume_spinbox.setSingleStep(5.0)
        self.volume_spinbox.setSuffix("%")
        self.volume_spinbox.setFixedWidth(76)
        self.fade_in_spinbox = QDoubleSpinBox()
        self.fade_in_spinbox.setRange(0.0, 600.0)
        self.fade_in_spinbox.setDecimals(2)
        self.fade_in_spinbox.setSingleStep(0.05)
        self.fade_in_spinbox.setSuffix(" s")
        self.fade_in_spinbox.setFixedWidth(82)
        self.fade_out_spinbox = QDoubleSpinBox()
        self.fade_out_spinbox.setRange(0.0, 600.0)
        self.fade_out_spinbox.setDecimals(2)
        self.fade_out_spinbox.setSingleStep(0.05)
        self.fade_out_spinbox.setSuffix(" s")
        self.fade_out_spinbox.setFixedWidth(82)

        self.auto_character_position_checkbox = QCheckBox("自動配置")
        self.position_x_spinbox = QDoubleSpinBox()
        self.position_x_spinbox.setRange(0.0, 100.0)
        self.position_x_spinbox.setDecimals(1)
        self.position_x_spinbox.setSuffix("%")
        self.position_x_spinbox.setFixedWidth(76)
        self.position_y_spinbox = QDoubleSpinBox()
        self.position_y_spinbox.setRange(0.0, 100.0)
        self.position_y_spinbox.setDecimals(1)
        self.position_y_spinbox.setSuffix("%")
        self.position_y_spinbox.setFixedWidth(76)
        self.character_scale_spinbox = QDoubleSpinBox()
        self.character_scale_spinbox.setRange(10.0, 250.0)
        self.character_scale_spinbox.setDecimals(0)
        self.character_scale_spinbox.setSingleStep(5.0)
        self.character_scale_spinbox.setSuffix("%")
        self.character_scale_spinbox.setFixedWidth(76)
        self.character_rotation_spinbox = QDoubleSpinBox()
        self.character_rotation_spinbox.setRange(-180.0, 180.0)
        self.character_rotation_spinbox.setDecimals(1)
        self.character_rotation_spinbox.setSuffix("°")
        self.character_rotation_spinbox.setFixedWidth(76)
        self.auto_character_position_checkbox.setChecked(True)
        for control in (
            self.auto_character_position_checkbox,
            self.position_x_spinbox,
            self.position_y_spinbox,
            self.character_scale_spinbox,
            self.character_rotation_spinbox,
        ):
            control.setEnabled(False)
        for control in (self.volume_spinbox, self.fade_in_spinbox, self.fade_out_spinbox):
            control.setEnabled(False)
            control.setKeyboardTracking(False)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.playback_button)
        toolbar.addWidget(self.stop_playback_button)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.add_clip_button)
        toolbar.addWidget(self.add_track_button)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.split_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.zoom_out_button)
        toolbar.addWidget(self.zoom_in_button)
        toolbar.addWidget(self.selection_label)
        toolbar.addStretch(1)
        toolbar.addWidget(self.status_label)

        self.canvas = TimelineCanvas(self.model)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(False)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: #151a22; }")

        audio_controls = QHBoxLayout()
        audio_controls.addWidget(QLabel("選択音声クリップ"))
        audio_controls.addWidget(QLabel("音量"))
        audio_controls.addWidget(self.volume_spinbox)
        audio_controls.addWidget(QLabel("フェードイン"))
        audio_controls.addWidget(self.fade_in_spinbox)
        audio_controls.addWidget(QLabel("フェードアウト"))
        audio_controls.addWidget(self.fade_out_spinbox)
        audio_controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(toolbar)
        character_controls = QHBoxLayout()
        character_controls.addWidget(QLabel("選択キャラクター"))
        character_controls.addWidget(self.auto_character_position_checkbox)
        character_controls.addWidget(QLabel("X"))
        character_controls.addWidget(self.position_x_spinbox)
        character_controls.addWidget(QLabel("Y"))
        character_controls.addWidget(self.position_y_spinbox)
        character_controls.addWidget(QLabel("サイズ"))
        character_controls.addWidget(self.character_scale_spinbox)
        character_controls.addWidget(QLabel("回転"))
        character_controls.addWidget(self.character_rotation_spinbox)
        character_controls.addStretch(1)

        layout.addLayout(audio_controls)
        layout.addLayout(character_controls)
        layout.addWidget(self.scroll, stretch=1)

        self.canvas.changed.connect(self._on_canvas_changed)
        self.canvas.playhead_changed.connect(self._on_playhead_changed)
        self.canvas.selection_changed.connect(self._on_selection_changed)
        self.playback_button.clicked.connect(self.playback_toggle_requested.emit)
        self.stop_playback_button.clicked.connect(self.playback_stop_requested.emit)
        self.add_clip_button.clicked.connect(self.add_clip_requested.emit)
        self.add_track_button.clicked.connect(self.add_track_requested.emit)
        self.split_button.clicked.connect(self.split_selected_at_playhead)
        self.delete_button.clicked.connect(self.delete_selected)
        self.zoom_out_button.clicked.connect(lambda: self._zoom(-12))
        self.zoom_in_button.clicked.connect(lambda: self._zoom(12))
        self.volume_spinbox.valueChanged.connect(self._apply_audio_controls)
        self.fade_in_spinbox.valueChanged.connect(self._apply_audio_controls)
        self.fade_out_spinbox.valueChanged.connect(self._apply_audio_controls)
        self.auto_character_position_checkbox.toggled.connect(self._apply_character_controls)
        self.position_x_spinbox.valueChanged.connect(self._apply_character_controls)
        self.position_y_spinbox.valueChanged.connect(self._apply_character_controls)
        self.character_scale_spinbox.valueChanged.connect(self._apply_character_controls)
        self.character_rotation_spinbox.valueChanged.connect(self._apply_character_controls)

    @property
    def selected_clip_id(self) -> str | None:
        return self.canvas.selected_clip_id

    @property
    def playhead_seconds(self) -> float:
        return self.canvas.playhead_seconds

    def _on_playhead_changed(self, seconds: float) -> None:
        self.playhead_changed.emit(seconds)

    def _on_selection_changed(self, clip_id: str) -> None:
        clip = self.model.get_clip(clip_id) if clip_id else None
        if clip is None:
            self.selection_label.setText("クリップ未選択")
            self.split_button.setEnabled(False)
            self.delete_button.setEnabled(False)
            self._sync_audio_controls(None, False)
            self._sync_character_controls(None, False)
            return
        track = self.model.get_track(clip.track_id)
        locked = track is None or track.locked
        suffix = "  [LOCKED]" if locked else ""
        self.selection_label.setText(f"{clip.name}  {clip.start:.2f}s–{clip.end:.2f}s{suffix}")
        self.split_button.setEnabled(not locked)
        self.delete_button.setEnabled(not locked)
        self._sync_audio_controls(
            clip,
            not locked and track is not None and track.kind == "audio"
            and clip.kind in {"audio", "voice"},
        )
        self._sync_character_controls(
            clip, not locked and clip.kind == "character"
        )

    def _sync_audio_controls(self, clip: TimelineClip | None, enabled: bool) -> None:
        controls = (self.volume_spinbox, self.fade_in_spinbox, self.fade_out_spinbox)
        blockers = [QSignalBlocker(control) for control in controls]
        for control in controls:
            control.setEnabled(enabled)
        if clip is None:
            self.volume_spinbox.setValue(100.0)
            self.fade_in_spinbox.setMaximum(600.0)
            self.fade_out_spinbox.setMaximum(600.0)
            self.fade_in_spinbox.setValue(0.0)
            self.fade_out_spinbox.setValue(0.0)
        else:
            self.volume_spinbox.setValue(clip.volume * 100.0)
            self.fade_in_spinbox.setMaximum(clip.duration)
            self.fade_out_spinbox.setMaximum(clip.duration)
            self.fade_in_spinbox.setValue(clip.fade_in)
            self.fade_out_spinbox.setValue(clip.fade_out)
        del blockers

    def _sync_character_controls(
        self, clip: TimelineClip | None, enabled: bool
    ) -> None:
        controls = (
            self.auto_character_position_checkbox,
            self.position_x_spinbox,
            self.position_y_spinbox,
            self.character_scale_spinbox,
            self.character_rotation_spinbox,
        )
        blockers = [QSignalBlocker(control) for control in controls]
        for control in controls:
            control.setEnabled(enabled)
        if clip is None:
            self.auto_character_position_checkbox.setChecked(True)
            self.position_x_spinbox.setValue(50.0)
            self.position_y_spinbox.setValue(50.0)
            self.character_scale_spinbox.setValue(100.0)
            self.character_rotation_spinbox.setValue(0.0)
        else:
            automatic = clip.position_x is None
            self.auto_character_position_checkbox.setChecked(automatic)
            self.position_x_spinbox.setValue(
                50.0 if automatic else float(clip.position_x) * 100.0
            )
            self.position_y_spinbox.setValue(
                50.0 if automatic else float(clip.position_y) * 100.0
            )
            self.character_scale_spinbox.setValue(clip.scale * 100.0)
            self.character_rotation_spinbox.setValue(clip.rotation_deg)
        self.position_x_spinbox.setEnabled(enabled and not self.auto_character_position_checkbox.isChecked())
        self.position_y_spinbox.setEnabled(enabled and not self.auto_character_position_checkbox.isChecked())
        del blockers

    def _apply_character_controls(self, _value=None) -> None:
        clip = self.model.get_clip(self.selected_clip_id or "")
        if clip is None or clip.kind != "character" or not self.character_scale_spinbox.isEnabled():
            return
        automatic = self.auto_character_position_checkbox.isChecked()
        try:
            self.model.set_character_transform(
                clip.id,
                position_x=None if automatic else self.position_x_spinbox.value() / 100.0,
                position_y=None if automatic else self.position_y_spinbox.value() / 100.0,
                scale=self.character_scale_spinbox.value() / 100.0,
                rotation_deg=self.character_rotation_spinbox.value(),
            )
        except TimelineError as exc:
            self.status_label.setText(str(exc))
            self._on_selection_changed(clip.id)
            return
        self.position_x_spinbox.setEnabled(not automatic)
        self.position_y_spinbox.setEnabled(not automatic)
        self.canvas.update()
        self.changed.emit()

    def _apply_audio_controls(self, _value: float = 0.0) -> None:
        clip = self.model.get_clip(self.selected_clip_id or "")
        if clip is None or not self.volume_spinbox.isEnabled():
            return
        try:
            self.model.set_audio_properties(
                clip.id,
                volume=self.volume_spinbox.value() / 100.0,
                fade_in=self.fade_in_spinbox.value(),
                fade_out=self.fade_out_spinbox.value(),
            )
        except TimelineError as exc:
            self.status_label.setText(str(exc))
            self._on_selection_changed(clip.id)
            return
        self.canvas.update()
        self.changed.emit()

    def _on_canvas_changed(self) -> None:
        self._on_selection_changed(self.selected_clip_id or "")
        self.changed.emit()

    def _zoom(self, delta: int) -> None:
        self.canvas.pixels_per_second = min(180, max(30, self.canvas.pixels_per_second + delta))
        self.canvas.refresh()

    def set_model(self, model: TimelineModel) -> None:
        self.model = model
        self.canvas.set_model(model)
        self._on_selection_changed("")

    def refresh(self) -> None:
        self.canvas.refresh()
        self._on_selection_changed(self.selected_clip_id or "")

    def set_playhead(self, seconds: float) -> None:
        self.canvas.set_playhead(seconds)

    def set_playback_state(self, playing: bool) -> None:
        self.playback_button.setText("Ⅱ 一時停止" if playing else "▶ 再生")

    def split_selected_at_playhead(self) -> None:
        clip_id = self.selected_clip_id
        if clip_id is None:
            self.status_label.setText("分割するクリップを選択してください")
            return
        selected_clip = self.model.get_clip(clip_id)
        selected_track = self.model.get_track(selected_clip.track_id) if selected_clip else None
        if selected_track is None or selected_track.locked:
            self.status_label.setText("ロックされたトラックのクリップは分割できません")
            return
        try:
            left, right = self.model.split_clip(clip_id, self.playhead_seconds)
        except TimelineError as exc:
            self.status_label.setText(str(exc))
            return
        self.canvas._set_selected_clip(right.id)
        self.refresh()
        self.changed.emit()
        self.status_label.setText("クリップを分割しました")

    def delete_selected(self) -> None:
        clip_id = self.selected_clip_id
        if clip_id is None:
            return
        selected_clip = self.model.get_clip(clip_id)
        selected_track = self.model.get_track(selected_clip.track_id) if selected_clip else None
        if selected_track is None or selected_track.locked:
            self.status_label.setText("ロックされたトラックのクリップは削除できません")
            return
        self.model.remove_clip(clip_id)
        self.canvas._set_selected_clip(None)
        self.refresh()
        self.changed.emit()
        self.status_label.setText("クリップを削除しました")

    def add_clip(
        self,
        track_id: str,
        kind: str,
        name: str,
        duration: float,
        *,
        start: float | None = None,
        source_in: float = 0.0,
        asset_id: str | None = None,
        script_line_id: str | None = None,
    ) -> TimelineClip:
        clip = self.model.add_clip(
            track_id,
            kind,
            name,
            duration,
            start=self.playhead_seconds if start is None else start,
            source_in=source_in,
            asset_id=asset_id,
            script_line_id=script_line_id,
        )
        self.canvas._set_selected_clip(clip.id)
        self.refresh()
        self.changed.emit()
        return clip

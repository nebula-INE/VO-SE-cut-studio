"""Keyframe editing helpers and a focused editor for aural-motion v1 clips."""

from __future__ import annotations

import math
from typing import Any

from character_motion import (
    CharacterMotionError,
    normalize_motion_keyframes,
    sample_motion,
)
from expression import normalize_name

try:
    from PySide6.QtCore import QSignalBlocker
    from PySide6.QtWidgets import (
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
    )
except ImportError:  # pragma: no cover - pure helpers remain importable without Qt
    QComboBox = QDialog = QDialogButtonBox = QDoubleSpinBox = None
    QFormLayout = QGridLayout = QGroupBox = QHBoxLayout = QLabel = None
    QMessageBox = QPushButton = QVBoxLayout = None
    QSignalBlocker = None


CORE_EXPRESSIONS = ("Joy", "Angry", "Sorrow", "Fun", "Blink")
EDITABLE_VOWELS = ("A", "I", "U", "E", "O")
EDITABLE_BONES = (
    "LeftUpperArm",
    "LeftLowerArm",
    "RightUpperArm",
    "RightLowerArm",
)
DEFAULT_UPPER_ARM_ROLL = {"LeftUpperArm": -70.0, "RightUpperArm": 70.0}


def _finite_time(value: Any) -> float:
    if isinstance(value, bool):
        raise CharacterMotionError("Keyframe time must be a finite non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CharacterMotionError(
            "Keyframe time must be a finite non-negative number"
        ) from exc
    if not math.isfinite(result) or result < 0.0:
        raise CharacterMotionError("Keyframe time must be a finite non-negative number")
    return result


def insert_motion_keyframe(
    keyframes: list[dict[str, Any]], time_sec: float
) -> tuple[list[dict[str, Any]], int]:
    """Insert the interpolated state at time_sec, or select a nearby existing key."""
    normalized = normalize_motion_keyframes(keyframes)
    timestamp = _finite_time(time_sec)
    for index, frame in enumerate(normalized):
        if math.isclose(frame["time"], timestamp, rel_tol=0.0, abs_tol=1e-6):
            return normalized, index

    state = sample_motion(normalized, timestamp)
    frame: dict[str, Any] = {
        "time": timestamp,
        "mouth_openness": state["mouth_openness"],
        "head": dict(state["head"]),
        "blend_shapes": dict(state["blend_shapes"]),
        "vowels": dict(state["vowels"]),
        "bones": {
            name: list(quaternion)
            for name, quaternion in state["bones"].items()
        },
    }
    normalized.append(frame)
    normalized.sort(key=lambda item: item["time"])
    index = next(i for i, item in enumerate(normalized) if item["time"] == timestamp)
    return normalized, index


def delete_motion_keyframe(
    keyframes: list[dict[str, Any]], index: int
) -> list[dict[str, Any]]:
    """Remove one keyframe while keeping the motion document valid."""
    normalized = normalize_motion_keyframes(keyframes)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(normalized):
        raise CharacterMotionError("Selected keyframe index is out of range")
    if len(normalized) <= 1:
        raise CharacterMotionError("A motion clip must keep at least one keyframe")
    del normalized[index]
    return normalized


def quaternion_roll_deg(quaternion: tuple[float, float, float, float]) -> float:
    """Return the Z-axis roll used by the built-in 2D arm renderer."""
    qx, qy, qz, qw = quaternion
    sinr_cosp = 2.0 * (qw * qz + qx * qy)
    cosr_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    return math.degrees(math.atan2(sinr_cosp, cosr_cosp))


def roll_quaternion(roll_deg: float) -> list[float]:
    """Convert an edited 2D arm roll into a normalized Z-axis quaternion."""
    half_angle = math.radians(float(roll_deg)) / 2.0
    return [0.0, 0.0, math.sin(half_angle), math.cos(half_angle)]


if QDialog is not None:

    class MotionKeyframeEditorDialog(QDialog):
        """Edit one keyframe at a time without loading thousands of table cells."""

        def __init__(
            self,
            keyframes: list[dict[str, Any]],
            insert_time: float,
            parent=None,
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle("モーションのキーフレーム編集")
            self.resize(760, 660)
            self.keyframes = normalize_motion_keyframes(keyframes)
            self._current_index = 0
            self._displayed_scalars: dict[str, float] = {}
            self._displayed_expressions: dict[str, float] = {}
            self._displayed_vowels: dict[str, float] = {}
            self._displayed_bone_rolls: dict[str, float] = {}

            # Keep the UI bounded even for hand-authored files with many custom
            # blend-shape names. Unlisted/custom names remain intact in the file.
            self.expression_names = (
                *CORE_EXPRESSIONS,
                "Blink_L",
                "Blink_R",
                "Surprised",
            )

            root = QVBoxLayout(self)
            selector_row = QHBoxLayout()
            selector_row.addWidget(QLabel("キーフレーム:"))
            self.frame_selector = QComboBox()
            self.frame_selector.currentIndexChanged.connect(self._on_frame_changed)
            selector_row.addWidget(self.frame_selector, 1)
            self._insert_time = _finite_time(insert_time)
            self.add_button = QPushButton("再生ヘッド位置に追加")
            self.add_button.clicked.connect(
                lambda: self._add_keyframe(self._insert_time)
            )
            selector_row.addWidget(self.add_button)
            self.delete_button = QPushButton("削除")
            self.delete_button.clicked.connect(self._delete_current_keyframe)
            selector_row.addWidget(self.delete_button)
            root.addLayout(selector_row)

            head_group = QGroupBox("頭部・口")
            head_form = QFormLayout(head_group)
            self.time_spin = self._spin(0.0, 1_000_000.0, 8, 0.01)
            self.head_x_spin = self._spin(-1_000_000.0, 1_000_000.0, 8, 0.01)
            self.head_y_spin = self._spin(-1_000_000.0, 1_000_000.0, 8, 0.01)
            self.tilt_spin = self._spin(-3600.0, 3600.0, 6, 0.5)
            self.mouth_spin = self._spin(0.0, 1.0, 6, 0.05)
            head_form.addRow("時間 (秒)", self.time_spin)
            head_form.addRow("頭部 X (m)", self.head_x_spin)
            head_form.addRow("頭部 Y (m)", self.head_y_spin)
            head_form.addRow("傾き (度)", self.tilt_spin)
            head_form.addRow("口の開き", self.mouth_spin)
            root.addWidget(head_group)

            face_row = QHBoxLayout()
            expression_group = QGroupBox("表情 (0〜1)")
            expression_grid = QGridLayout(expression_group)
            self.expression_spins: dict[str, QDoubleSpinBox] = {}
            for index, name in enumerate(self.expression_names):
                spin = self._spin(0.0, 1.0, 6, 0.05)
                self.expression_spins[name] = spin
                expression_grid.addWidget(QLabel(name), index, 0)
                expression_grid.addWidget(spin, index, 1)
            face_row.addWidget(expression_group)

            vowel_group = QGroupBox("母音 (0〜1)")
            vowel_grid = QGridLayout(vowel_group)
            self.vowel_spins: dict[str, QDoubleSpinBox] = {}
            for index, name in enumerate(EDITABLE_VOWELS):
                spin = self._spin(0.0, 1.0, 6, 0.05)
                self.vowel_spins[name] = spin
                vowel_grid.addWidget(QLabel(name), index, 0)
                vowel_grid.addWidget(spin, index, 1)
            face_row.addWidget(vowel_group)
            root.addLayout(face_row)

            arm_group = QGroupBox("腕の角度 (Zロール・度)")
            arm_grid = QGridLayout(arm_group)
            self.bone_spins: dict[str, QDoubleSpinBox] = {}
            for index, name in enumerate(EDITABLE_BONES):
                spin = self._spin(-360.0, 360.0, 4, 1.0)
                self.bone_spins[name] = spin
                arm_grid.addWidget(QLabel(name), index // 2, (index % 2) * 2)
                arm_grid.addWidget(spin, index // 2, (index % 2) * 2 + 1)
            root.addWidget(arm_group)

            note = QLabel(
                "腕の角度を変更した場合は、2D描画用のZ軸クォータニオンとして保存します。"
                "変更しない腕の元データはそのまま保持します。"
            )
            note.setWordWrap(True)
            root.addWidget(note)

            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(self._accept_changes)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)

            selected = min(
                range(len(self.keyframes)),
                key=lambda index: abs(self.keyframes[index]["time"] - self._insert_time),
            )
            self._rebuild_selector(selected)

        @staticmethod
        def _spin(minimum: float, maximum: float, decimals: int, step: float):
            spin = QDoubleSpinBox()
            spin.setRange(minimum, maximum)
            spin.setDecimals(decimals)
            spin.setSingleStep(step)
            spin.setKeyboardTracking(False)
            return spin

        def _rebuild_selector(self, selected_index: int) -> None:
            blocker = QSignalBlocker(self.frame_selector)
            self.frame_selector.clear()
            for index, frame in enumerate(self.keyframes):
                self.frame_selector.addItem(
                    f"{index + 1}: {frame['time']:.3f} 秒"
                )
            selected_index = max(0, min(selected_index, len(self.keyframes) - 1))
            self.frame_selector.setCurrentIndex(selected_index)
            del blocker
            self._current_index = selected_index
            self._load_current_frame()
            self.delete_button.setEnabled(len(self.keyframes) > 1)

        def _on_frame_changed(self, index: int) -> None:
            if index < 0 or index == self._current_index:
                return
            self._commit_current_frame()
            self._current_index = index
            self._load_current_frame()

        def _load_current_frame(self) -> None:
            frame = self.keyframes[self._current_index]
            head = frame.get("head", {})
            self.time_spin.setValue(float(frame["time"]))
            self.head_x_spin.setValue(float(head.get("x", 0.0)))
            self.head_y_spin.setValue(float(head.get("y", 0.0)))
            self.tilt_spin.setValue(float(head.get("tilt_deg", 0.0)))
            self.mouth_spin.setValue(float(frame.get("mouth_openness", 0.0)))
            self._displayed_scalars = {
                "time": float(self.time_spin.value()),
                "head.x": float(self.head_x_spin.value()),
                "head.y": float(self.head_y_spin.value()),
                "head.tilt_deg": float(self.tilt_spin.value()),
                "mouth_openness": float(self.mouth_spin.value()),
            }

            shapes = frame.get("blend_shapes", {})
            canonical_shapes: dict[str, float] = {}
            for raw_name, value in shapes.items():
                name = normalize_name(raw_name)
                canonical_shapes[name] = max(
                    canonical_shapes.get(name, 0.0), float(value)
                )
            self._displayed_expressions = {}
            for name, spin in self.expression_spins.items():
                spin.setValue(float(canonical_shapes.get(name, 0.0)))
                self._displayed_expressions[name] = float(spin.value())

            vowels = frame.get("vowels", {})
            self._displayed_vowels = {}
            for name, spin in self.vowel_spins.items():
                spin.setValue(float(vowels.get(name, 0.0)))
                self._displayed_vowels[name] = float(spin.value())

            self._displayed_bone_rolls = {}
            bones = frame.get("bones", {})
            for name, spin in self.bone_spins.items():
                original = bones.get(name)
                if original is not None:
                    roll = quaternion_roll_deg(tuple(float(v) for v in original))
                else:
                    roll = float(DEFAULT_UPPER_ARM_ROLL.get(name, 0.0))
                spin.setValue(roll)
                # Compare against the value actually shown by the spin box. This
                # avoids rewriting high-precision quaternions due to display rounding.
                self._displayed_bone_rolls[name] = float(spin.value())

        def _commit_current_frame(self) -> None:
            frame = self.keyframes[self._current_index]

            current_time = float(self.time_spin.value())
            if not math.isclose(
                current_time, self._displayed_scalars["time"],
                rel_tol=0.0, abs_tol=1e-12,
            ):
                frame["time"] = current_time

            head = dict(frame.get("head", {}))
            for key, spin in (
                ("x", self.head_x_spin),
                ("y", self.head_y_spin),
                ("tilt_deg", self.tilt_spin),
            ):
                scalar_key = f"head.{key}"
                current_value = float(spin.value())
                if not math.isclose(
                    current_value, self._displayed_scalars[scalar_key],
                    rel_tol=0.0, abs_tol=1e-12,
                ):
                    head[key] = current_value
            if head:
                frame["head"] = head

            current_mouth = float(self.mouth_spin.value())
            if not math.isclose(
                current_mouth, self._displayed_scalars["mouth_openness"],
                rel_tol=0.0, abs_tol=1e-12,
            ):
                frame["mouth_openness"] = current_mouth

            shapes = dict(frame.get("blend_shapes", {}))
            previously_present = {
                normalize_name(raw_name) for raw_name in shapes
            }
            for name, spin in self.expression_spins.items():
                value = float(spin.value())
                if math.isclose(
                    value, self._displayed_expressions[name],
                    rel_tol=0.0, abs_tol=1e-12,
                ):
                    continue
                for raw_name in list(shapes):
                    if normalize_name(raw_name) == name:
                        shapes.pop(raw_name)
                if value > 0.0 or name in previously_present:
                    shapes[name] = value
            if shapes:
                frame["blend_shapes"] = shapes

            vowels = dict(frame.get("vowels", {}))
            for name, spin in self.vowel_spins.items():
                value = float(spin.value())
                if math.isclose(
                    value, self._displayed_vowels[name],
                    rel_tol=0.0, abs_tol=1e-12,
                ):
                    continue
                if value > 0.0 or name in vowels:
                    vowels[name] = value
                else:
                    vowels.pop(name, None)
            if vowels:
                frame["vowels"] = vowels

            bones = dict(frame.get("bones", {}))
            for name, spin in self.bone_spins.items():
                current_roll = float(spin.value())
                previous_roll = self._displayed_bone_rolls[name]
                if not math.isclose(
                    current_roll, previous_roll, rel_tol=0.0, abs_tol=1e-12
                ):
                    bones[name] = roll_quaternion(current_roll)
            if bones:
                frame["bones"] = bones

        def _add_keyframe(self, time_sec: float) -> None:
            self._commit_current_frame()
            try:
                self.keyframes, index = insert_motion_keyframe(self.keyframes, time_sec)
            except CharacterMotionError as exc:
                QMessageBox.warning(self, "キーフレームを追加できません", str(exc))
                return
            self._rebuild_selector(index)

        def _delete_current_keyframe(self) -> None:
            self._commit_current_frame()
            try:
                self.keyframes = delete_motion_keyframe(
                    self.keyframes, self._current_index
                )
            except CharacterMotionError as exc:
                QMessageBox.warning(self, "キーフレームを削除できません", str(exc))
                return
            self._rebuild_selector(min(self._current_index, len(self.keyframes) - 1))

        def _accept_changes(self) -> None:
            self._commit_current_frame()
            try:
                self.keyframes = normalize_motion_keyframes(self.keyframes)
            except CharacterMotionError as exc:
                QMessageBox.warning(self, "キーフレームに問題があります", str(exc))
                return
            super().accept()

else:

    class MotionKeyframeEditorDialog:  # pragma: no cover - optional GUI dependency
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PySide6 is required for the motion keyframe editor")

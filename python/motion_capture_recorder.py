"""30-fps live VMC/WebSocket motion snapshot recorder."""
from __future__ import annotations

import math
import time
from typing import Callable

from character_motion import SUPPORTED_BONES
from expression import normalize_blend_shapes
from vrm_mocap import SUPPORTED_VRM_BONES

FRAME_RATE = 30.0
MAX_RECORDING_SECONDS = 300.0
_SUPPORTED_SHAPES = frozenset({
    "Joy", "Angry", "Sorrow", "Fun", "Blink", "Blink_L", "Blink_R", "Surprised",
})
_SUPPORTED_VOWELS = frozenset({"A", "I", "U", "E", "O"})


class MotionCaptureRecordingError(ValueError):
    """Raised when a live motion recording cannot continue."""


def _finite(value: float, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MotionCaptureRecordingError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise MotionCaptureRecordingError(f"{label} must be finite")
    return result


class MotionCaptureRecorder:
    """Collect bounded snapshots on the GUI thread using monotonic timestamps."""

    def __init__(
        self,
        frame_rate: float = FRAME_RATE,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.frame_rate = _finite(frame_rate, "frame_rate")
        if not 1.0 <= self.frame_rate <= 120.0:
            raise MotionCaptureRecordingError("frame_rate must be between 1 and 120")
        self._clock = clock
        self._recording = False
        self._started_at = 0.0
        self._last_capture = 0.0
        self._head = {"x": 0.0, "y": 0.0, "tilt_deg": 0.0}
        self._shapes: dict[str, float] = {}
        self._vowels: dict[str, float] = {}
        self._bones: dict[str, tuple[float, float, float, float]] = {}
        self._head_3d: dict | None = None
        self._vrm_bones: dict[str, dict] = {}
        self._frames: list[dict] = []

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def keyframe_count(self) -> int:
        return len(self._frames)

    def start(self, *, now: float | None = None) -> None:
        if self._recording:
            raise MotionCaptureRecordingError("A recording is already active")
        self._started_at = self._clock() if now is None else _finite(now, "start time")
        self._last_capture = 0.0
        self._head = {"x": 0.0, "y": 0.0, "tilt_deg": 0.0}
        self._shapes, self._vowels, self._bones, self._frames = {}, {}, {}, []
        self._head_3d = None
        self._vrm_bones = {}
        self._recording = True
        self._frames.append(self._snapshot(0.0))

    def update_head(self, x: float, y: float, tilt_deg: float) -> None:
        if self._recording:
            self._head = {
                "x": _finite(x, "head.x"),
                "y": _finite(y, "head.y"),
                "tilt_deg": _finite(tilt_deg, "head.tilt_deg"),
            }

    def update_vrm_head(
        self,
        x: float, y: float, z: float,
        qx: float, qy: float, qz: float, qw: float,
    ) -> None:
        """Update the full 3D head transform used by VRM 0.x/1.0 playback."""
        if not self._recording:
            return
        position = tuple(
            _finite(value, f"head_3d.{axis}")
            for axis, value in zip(("x", "y", "z"), (x, y, z))
        )
        rotation = tuple(
            _finite(value, "head_3d quaternion")
            for value in (qx, qy, qz, qw)
        )
        norm = math.sqrt(sum(value * value for value in rotation))
        if norm < 1e-8:
            return
        self._head_3d = {
            "position": list(position),
            "rotation": [value / norm for value in rotation],
        }

    def update_vrm_bone(
        self,
        name: str,
        x: float, y: float, z: float,
        qx: float, qy: float, qz: float, qw: float,
    ) -> None:
        """Record raw VMC positions and normalized rotations for VRM humanoid bones."""
        if not self._recording or name not in SUPPORTED_VRM_BONES or name == "Head":
            return
        position = tuple(
            _finite(value, f"{name} position")
            for value in (x, y, z)
        )
        rotation = tuple(
            _finite(value, f"{name} quaternion")
            for value in (qx, qy, qz, qw)
        )
        norm = math.sqrt(sum(value * value for value in rotation))
        if norm < 1e-8:
            return
        self._vrm_bones[name] = {
            "position": list(position),
            "rotation": [value / norm for value in rotation],
        }

    def update_blend_shapes(self, values: dict[str, float]) -> None:
        if not self._recording or not isinstance(values, dict):
            return
        normalized = normalize_blend_shapes(values)
        self._shapes = {
            key: value for key, value in normalized.items()
            if key in _SUPPORTED_SHAPES and math.isfinite(value)
        }
        self._vowels = {
            key: value for key, value in normalized.items()
            if key in _SUPPORTED_VOWELS and math.isfinite(value)
        }

    def update_bone(self, name: str, qx: float, qy: float, qz: float, qw: float) -> None:
        if not self._recording or name not in SUPPORTED_BONES:
            return
        values = tuple(
            _finite(value, f"{name} quaternion")
            for value in (qx, qy, qz, qw)
        )
        norm = math.sqrt(sum(value * value for value in values))
        if norm < 1e-8:
            return
        self._bones[name] = tuple(value / norm for value in values)

    def _snapshot(self, elapsed: float) -> dict:
        frame = {
            "time": round(max(0.0, elapsed), 6),
            "head": dict(self._head),
            "blend_shapes": dict(self._shapes),
            "vowels": dict(self._vowels),
            "bones": {key: list(value) for key, value in sorted(self._bones.items())},
        }
        if self._head_3d is not None:
            frame["head_3d"] = {
                "position": list(self._head_3d["position"]),
                "rotation": list(self._head_3d["rotation"]),
            }
        if self._vrm_bones:
            frame["vrm_bones"] = {
                name: {
                    "position": list(transform["position"]),
                    "rotation": list(transform["rotation"]),
                }
                for name, transform in sorted(self._vrm_bones.items())
            }
        return frame

    def capture(self, *, now: float | None = None, force: bool = False) -> bool:
        if not self._recording:
            return False
        current = self._clock() if now is None else _finite(now, "capture time")
        elapsed = current - self._started_at
        if elapsed < 0:
            raise MotionCaptureRecordingError("capture time precedes start time")
        if elapsed > MAX_RECORDING_SECONDS:
            raise MotionCaptureRecordingError(
                f"Maximum recording length is {MAX_RECORDING_SECONDS:g} seconds"
            )
        if not force and elapsed - self._last_capture < 0.9 / self.frame_rate:
            return False
        self._last_capture = elapsed
        frame = self._snapshot(elapsed)
        if self._frames and self._state(frame) == self._state(self._frames[-1]):
            return False
        if self._frames and frame["time"] <= self._frames[-1]["time"]:
            frame["time"] = round(self._frames[-1]["time"] + 1e-6, 6)
        self._frames.append(frame)
        return True

    @staticmethod
    def _state(frame: dict) -> tuple:
        return (
            tuple(sorted(frame["head"].items())),
            tuple(sorted(frame["blend_shapes"].items())),
            tuple(sorted(frame["vowels"].items())),
            tuple((key, tuple(value)) for key, value in sorted(frame["bones"].items())),
            repr(frame.get("head_3d")),
            tuple(
                (key, tuple(value["position"]), tuple(value["rotation"]))
                for key, value in sorted(frame.get("vrm_bones", {}).items())
            ),
        )

    def stop(self, *, now: float | None = None) -> list[dict]:
        if not self._recording:
            raise MotionCaptureRecordingError("No recording is active")
        current = self._clock() if now is None else _finite(now, "stop time")
        elapsed = current - self._started_at
        if elapsed < 0:
            raise MotionCaptureRecordingError("stop time precedes start time")
        elapsed = max(0.05, min(elapsed, MAX_RECORDING_SECONDS))
        try:
            self.capture(now=self._started_at + elapsed, force=True)
            if self._frames[-1]["time"] < elapsed:
                self._frames.append(self._snapshot(elapsed))
            return [
                {
                    **frame,
                    "head": dict(frame["head"]),
                    "blend_shapes": dict(frame["blend_shapes"]),
                    "vowels": dict(frame["vowels"]),
                    "bones": {key: list(value) for key, value in frame["bones"].items()},
                    **({
                        "head_3d": {
                            "position": list(frame["head_3d"]["position"]),
                            "rotation": list(frame["head_3d"]["rotation"]),
                        }
                    } if "head_3d" in frame else {}),
                    **({
                        "vrm_bones": {
                            name: {
                                "position": list(transform["position"]),
                                "rotation": list(transform["rotation"]),
                            }
                            for name, transform in frame["vrm_bones"].items()
                        }
                    } if "vrm_bones" in frame else {}),
                }
                for frame in self._frames
            ]
        finally:
            self._recording = False

    def cancel(self) -> None:
        self._recording = False
        self._frames.clear()
        self._shapes.clear()
        self._vowels.clear()
        self._bones.clear()
        self._head_3d = None
        self._vrm_bones.clear()

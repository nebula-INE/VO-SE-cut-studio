"""Validated keyframe data for offline 2D and VRM character animation.

Motion files are simple UTF-8 JSON documents so recordings can be created by
mocap tools, scripts, or hand-authored without a Qt dependency.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

MOTION_FORMAT = "aural-motion"
MOTION_VERSION = 2
LEGACY_MOTION_VERSION = 1
MAX_MOTION_FILE_BYTES = 10 * 1024 * 1024
SUPPORTED_BONES = frozenset({
    "LeftUpperArm", "LeftLowerArm", "RightUpperArm", "RightLowerArm",
})
SUPPORTED_VRM_BONES = frozenset({
    "hips", "spine", "chest", "upperChest", "neck", "head",
    "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
    "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes",
    "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes",
    "leftEye", "rightEye", "jaw",
    "leftThumbMetacarpal", "leftThumbProximal", "leftThumbDistal",
    "leftIndexProximal", "leftIndexIntermediate", "leftIndexDistal",
    "leftMiddleProximal", "leftMiddleIntermediate", "leftMiddleDistal",
    "leftRingProximal", "leftRingIntermediate", "leftRingDistal",
    "leftLittleProximal", "leftLittleIntermediate", "leftLittleDistal",
    "rightThumbMetacarpal", "rightThumbProximal", "rightThumbDistal",
    "rightIndexProximal", "rightIndexIntermediate", "rightIndexDistal",
    "rightMiddleProximal", "rightMiddleIntermediate", "rightMiddleDistal",
    "rightRingProximal", "rightRingIntermediate", "rightRingDistal",
    "rightLittleProximal", "rightLittleIntermediate", "rightLittleDistal",
})
SUPPORTED_VOWELS = frozenset({"A", "I", "U", "E", "O"})


class CharacterMotionError(ValueError):
    """Raised when an offline character motion document is invalid."""


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise CharacterMotionError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CharacterMotionError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise CharacterMotionError(f"{label} must be a finite number")
    if minimum is not None and number < minimum:
        raise CharacterMotionError(f"{label} must be at least {minimum}")
    return number


def _weight_map(value: Any, label: str, allowed: frozenset[str] | None = None) -> dict[str, float]:
    if not isinstance(value, dict):
        raise CharacterMotionError(f"{label} must be an object")
    result: dict[str, float] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not name.strip():
            raise CharacterMotionError(f"{label} contains an empty name")
        if allowed is not None and name not in allowed:
            raise CharacterMotionError(f"Unsupported {label} name: {name}")
        weight = _finite_number(raw, f"{label}.{name}")
        if not 0.0 <= weight <= 1.0:
            raise CharacterMotionError(f"{label}.{name} must be between 0 and 1")
        result[name] = weight
    return result


def _quaternion(value: Any, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CharacterMotionError(f"{label} must be [qx, qy, qz, qw]")
    components = [_finite_number(item, label) for item in value]
    length = math.sqrt(sum(item * item for item in components))
    if length < 1e-8:
        raise CharacterMotionError(f"{label} cannot be a zero quaternion")
    return tuple(item / length for item in components)  # type: ignore[return-value]


def _vector3(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise CharacterMotionError(f"{label} must be [x, y, z]")
    return tuple(_finite_number(item, label) for item in value)  # type: ignore[return-value]


def _normalize_vrm_transform(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CharacterMotionError(f"{label} must be an object")
    result: dict[str, Any] = {}
    if "position" in raw:
        result["position"] = _vector3(raw["position"], f"{label}.position")
    if "rotation" in raw:
        result["rotation"] = _quaternion(raw["rotation"], f"{label}.rotation")
    if not result:
        raise CharacterMotionError(f"{label} must contain position or rotation")
    return result


def _normalize_keyframe(raw: Any, index: int) -> dict[str, Any]:
    label = f"keyframes[{index}]"
    if not isinstance(raw, dict):
        raise CharacterMotionError(f"{label} must be an object")
    timestamp = _finite_number(raw.get("time"), f"{label}.time", minimum=0.0)
    result: dict[str, Any] = {"time": timestamp}

    if "mouth_openness" in raw:
        openness = _finite_number(raw["mouth_openness"], f"{label}.mouth_openness")
        if not 0.0 <= openness <= 1.0:
            raise CharacterMotionError(f"{label}.mouth_openness must be between 0 and 1")
        result["mouth_openness"] = openness

    head = raw.get("head", {})
    if not isinstance(head, dict):
        raise CharacterMotionError(f"{label}.head must be an object")
    normalized_head = {}
    for key in ("x", "y", "tilt_deg"):
        if key in head:
            normalized_head[key] = _finite_number(head[key], f"{label}.head.{key}")
    if normalized_head:
        result["head"] = normalized_head

    if "blend_shapes" in raw:
        result["blend_shapes"] = _weight_map(raw["blend_shapes"], f"{label}.blend_shapes")
    if "vowels" in raw:
        result["vowels"] = _weight_map(raw["vowels"], f"{label}.vowels", SUPPORTED_VOWELS)

    bones = raw.get("bones", {})
    if not isinstance(bones, dict):
        raise CharacterMotionError(f"{label}.bones must be an object")
    normalized_bones = {}
    for name, value in bones.items():
        if name not in SUPPORTED_BONES:
            raise CharacterMotionError(f"Unsupported 2D bone name: {name}")
        normalized_bones[name] = _quaternion(value, f"{label}.bones.{name}")
    if normalized_bones:
        result["bones"] = normalized_bones

    vrm = raw.get("vrm", {})
    if not isinstance(vrm, dict):
        raise CharacterMotionError(f"{label}.vrm must be an object")
    normalized_vrm: dict[str, Any] = {}
    if "head" in vrm:
        normalized_vrm["head"] = _normalize_vrm_transform(
            vrm["head"], f"{label}.vrm.head"
        )
    vrm_bones = vrm.get("bones", {})
    if not isinstance(vrm_bones, dict):
        raise CharacterMotionError(f"{label}.vrm.bones must be an object")
    normalized_vrm_bones = {}
    for name, transform in vrm_bones.items():
        if name not in SUPPORTED_VRM_BONES:
            raise CharacterMotionError(f"Unsupported VRM humanoid bone name: {name}")
        normalized_vrm_bones[name] = _normalize_vrm_transform(
            transform, f"{label}.vrm.bones.{name}"
        )
    if normalized_vrm_bones:
        normalized_vrm["bones"] = normalized_vrm_bones
    if normalized_vrm:
        result["vrm"] = normalized_vrm
    return result


def load_motion_file(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a backward-compatible aural-motion v1/v2 JSON file."""
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
        if size > MAX_MOTION_FILE_BYTES:
            raise CharacterMotionError(
                f"Motion file is too large ({size} bytes; limit {MAX_MOTION_FILE_BYTES})"
            )
        payload = json.loads(source.read_text(encoding="utf-8"))
    except CharacterMotionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CharacterMotionError(f"Could not read motion file {source}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("format") != MOTION_FORMAT:
        raise CharacterMotionError(f"Motion file must have format={MOTION_FORMAT!r}")
    version = payload.get("version")
    if type(version) is not int or version not in (LEGACY_MOTION_VERSION, MOTION_VERSION):
        raise CharacterMotionError(f"Unsupported motion format version: {version!r}")
    frames = payload.get("keyframes")
    if not isinstance(frames, list) or not frames:
        raise CharacterMotionError("Motion file must contain at least one keyframe")

    normalized = [_normalize_keyframe(frame, index) for index, frame in enumerate(frames)]
    normalized.sort(key=lambda frame: frame["time"])
    times = [frame["time"] for frame in normalized]
    if len(times) != len(set(times)):
        raise CharacterMotionError("Motion keyframe times must be unique")
    return normalized


def _lerp(left: float, right: float, amount: float) -> float:
    return left + (right - left) * amount


def _slerp(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    amount: float,
) -> tuple[float, float, float, float]:
    dot = sum(a * b for a, b in zip(left, right))
    if dot < 0.0:
        right = tuple(-value for value in right)
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        blended = tuple(_lerp(a, b, amount) for a, b in zip(left, right))
        length = math.sqrt(sum(value * value for value in blended))
        return tuple(value / length for value in blended)  # type: ignore[return-value]
    theta = math.acos(dot)
    sine = math.sin(theta)
    left_weight = math.sin((1.0 - amount) * theta) / sine
    right_weight = math.sin(amount * theta) / sine
    return tuple(left_weight * a + right_weight * b for a, b in zip(left, right))  # type: ignore[return-value]


def _interpolate_vrm_transform(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    amount: float,
) -> dict[str, Any]:
    if left is None and right is None:
        return {}
    left_values = left or {}
    right_values = right or {}
    result: dict[str, Any] = {}

    # Recordings start with an empty snapshot before the first incoming VMC
    # packet. Blend that first packet from a neutral transform instead of
    # snapping the avatar into the pose before the first recorded timestamp.
    if "position" in right_values:
        start_position = left_values.get("position", (0.0, 0.0, 0.0))
        result["position"] = tuple(
            _lerp(a, b, amount)
            for a, b in zip(start_position, right_values["position"])
        )
    elif "position" in left_values:
        result["position"] = left_values["position"]

    if "rotation" in right_values:
        start_rotation = left_values.get("rotation", (0.0, 0.0, 0.0, 1.0))
        result["rotation"] = _slerp(start_rotation, right_values["rotation"], amount)
    elif "rotation" in left_values:
        result["rotation"] = left_values["rotation"]
    return result


def _interpolate_vrm(
    left: dict[str, Any],
    right: dict[str, Any],
    amount: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    head = _interpolate_vrm_transform(left.get("head"), right.get("head"), amount)
    if head:
        result["head"] = head
    left_bones = left.get("bones", {})
    right_bones = right.get("bones", {})
    bones = {}
    for name in set(left_bones) | set(right_bones):
        transform = _interpolate_vrm_transform(
            left_bones.get(name), right_bones.get(name), amount
        )
        if transform:
            bones[name] = transform
    if bones:
        result["bones"] = bones
    return result


def sample_motion(keyframes: list[dict[str, Any]], time_sec: float) -> dict[str, Any]:
    """Interpolate one animation state at a source-relative timestamp."""
    if not keyframes:
        return {
            "mouth_openness": 0.0,
            "head": {"x": 0.0, "y": 0.0, "tilt_deg": 0.0},
            "blend_shapes": {},
            "vowels": {},
            "bones": {},
            "vrm": {},
        }
    timestamp = max(0.0, _finite_number(time_sec, "motion sample time"))
    right_index = next(
        (index for index, frame in enumerate(keyframes) if frame["time"] >= timestamp),
        len(keyframes) - 1,
    )
    right = keyframes[right_index]
    left = keyframes[max(0, right_index - 1)]
    if right["time"] <= left["time"] or timestamp <= left["time"]:
        amount = 0.0
    elif timestamp >= right["time"]:
        amount = 1.0
    else:
        amount = (timestamp - left["time"]) / (right["time"] - left["time"])

    mouth = _lerp(
        float(left.get("mouth_openness", 0.0)),
        float(right.get("mouth_openness", left.get("mouth_openness", 0.0))),
        amount,
    )
    head = {}
    for key in ("x", "y", "tilt_deg"):
        left_value = float(left.get("head", {}).get(key, 0.0))
        right_value = float(right.get("head", {}).get(key, left_value))
        head[key] = _lerp(left_value, right_value, amount)

    def interpolate_weights(field: str) -> dict[str, float]:
        left_values = left.get(field, {})
        right_values = right.get(field, {})
        keys = set(left_values) | set(right_values)
        return {
            key: _lerp(
                float(left_values.get(key, 0.0)),
                float(right_values.get(key, left_values.get(key, 0.0))),
                amount,
            )
            for key in keys
        }

    left_bones = left.get("bones", {})
    right_bones = right.get("bones", {})
    bones = {}
    for name in set(left_bones) | set(right_bones):
        if name not in left_bones:
            bones[name] = right_bones[name]
        elif name not in right_bones:
            bones[name] = left_bones[name]
        else:
            bones[name] = _slerp(left_bones[name], right_bones[name], amount)

    left_vrm = left.get("vrm", {})
    right_vrm = right.get("vrm", {})
    vrm = _interpolate_vrm(left_vrm, right_vrm, amount)

    return {
        "mouth_openness": max(0.0, min(1.0, mouth)),
        "head": head,
        "blend_shapes": interpolate_weights("blend_shapes"),
        "vowels": interpolate_weights("vowels"),
        "bones": bones,
        "vrm": vrm,
    }


def motion_duration(keyframes: list[dict[str, Any]]) -> float:
    return float(keyframes[-1]["time"]) if keyframes else 0.0



def save_motion_file(path: str | Path, keyframes: list[dict[str, Any]]) -> Path:
    """Validate and atomically save an aural-motion v1 or v2 JSON file."""
    destination = Path(path).expanduser()
    if not isinstance(keyframes, list) or not keyframes:
        raise CharacterMotionError("Motion recording must contain at least one keyframe")
    normalized = [
        _normalize_keyframe(frame, index)
        for index, frame in enumerate(keyframes)
    ]
    normalized.sort(key=lambda frame: frame["time"])
    times = [frame["time"] for frame in normalized]
    if len(times) != len(set(times)):
        raise CharacterMotionError("Motion keyframe times must be unique")
    contains_vrm = any("vrm" in frame for frame in normalized)
    payload = {
        "format": MOTION_FORMAT,
        "version": MOTION_VERSION if contains_vrm else LEGACY_MOTION_VERSION,
        "keyframes": normalized,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_MOTION_FILE_BYTES:
        raise CharacterMotionError(
            f"Motion file is too large ({len(encoded)} bytes; limit {MAX_MOTION_FILE_BYTES})"
        )

    temporary_name: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except OSError as exc:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise CharacterMotionError(
            f"Could not save motion file {destination}: {exc}"
        ) from exc
    return destination

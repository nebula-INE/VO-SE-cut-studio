"""Validated VMC-to-VRM humanoid and expression name mapping."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

_VMC_BONE_MAP = {
    "Hips": "hips",
    "Spine": "spine",
    "Chest": "chest",
    "UpperChest": "upperChest",
    "Neck": "neck",
    "Head": "head",
    "LeftShoulder": "leftShoulder",
    "LeftUpperArm": "leftUpperArm",
    "LeftLowerArm": "leftLowerArm",
    "LeftHand": "leftHand",
    "RightShoulder": "rightShoulder",
    "RightUpperArm": "rightUpperArm",
    "RightLowerArm": "rightLowerArm",
    "RightHand": "rightHand",
    "LeftUpperLeg": "leftUpperLeg",
    "LeftLowerLeg": "leftLowerLeg",
    "LeftFoot": "leftFoot",
    "LeftToes": "leftToes",
    "RightUpperLeg": "rightUpperLeg",
    "RightLowerLeg": "rightLowerLeg",
    "RightFoot": "rightFoot",
    "RightToes": "rightToes",
    "LeftEye": "leftEye",
    "RightEye": "rightEye",
    "Jaw": "jaw",
    "LeftThumbMetacarpal": "leftThumbMetacarpal",
    "LeftThumbProximal": "leftThumbProximal",
    "LeftThumbDistal": "leftThumbDistal",
    "LeftIndexProximal": "leftIndexProximal",
    "LeftIndexIntermediate": "leftIndexIntermediate",
    "LeftIndexDistal": "leftIndexDistal",
    "LeftMiddleProximal": "leftMiddleProximal",
    "LeftMiddleIntermediate": "leftMiddleIntermediate",
    "LeftMiddleDistal": "leftMiddleDistal",
    "LeftRingProximal": "leftRingProximal",
    "LeftRingIntermediate": "leftRingIntermediate",
    "LeftRingDistal": "leftRingDistal",
    "LeftLittleProximal": "leftLittleProximal",
    "LeftLittleIntermediate": "leftLittleIntermediate",
    "LeftLittleDistal": "leftLittleDistal",
    "RightThumbMetacarpal": "rightThumbMetacarpal",
    "RightThumbProximal": "rightThumbProximal",
    "RightThumbDistal": "rightThumbDistal",
    "RightIndexProximal": "rightIndexProximal",
    "RightIndexIntermediate": "rightIndexIntermediate",
    "RightIndexDistal": "rightIndexDistal",
    "RightMiddleProximal": "rightMiddleProximal",
    "RightMiddleIntermediate": "rightMiddleIntermediate",
    "RightMiddleDistal": "rightMiddleDistal",
    "RightRingProximal": "rightRingProximal",
    "RightRingIntermediate": "rightRingIntermediate",
    "RightRingDistal": "rightRingDistal",
    "RightLittleProximal": "rightLittleProximal",
    "RightLittleIntermediate": "rightLittleIntermediate",
    "RightLittleDistal": "rightLittleDistal",
}

_VMC_EXPRESSION_MAP = {
    "joy": "happy",
    "happy": "happy",
    "angry": "angry",
    "sorrow": "sad",
    "sad": "sad",
    "fun": "relaxed",
    "relaxed": "relaxed",
    "surprised": "surprised",
    "blink": "blink",
    "blink_l": "blinkLeft",
    "blink_r": "blinkRight",
    "a": "aa",
    "aa": "aa",
    "i": "ih",
    "ih": "ih",
    "u": "ou",
    "ou": "ou",
    "e": "ee",
    "ee": "ee",
    "o": "oh",
    "oh": "oh",
}


def vmc_bone_to_vrm(name: str) -> str | None:
    """Return the VRM normalized humanoid bone name for a VMC bone name."""
    return _VMC_BONE_MAP.get(str(name).strip())


def normalize_quaternion(values: Sequence[float]) -> tuple[float, float, float, float]:
    """Normalize a finite XYZW quaternion; zero-length input becomes identity."""
    if len(values) != 4:
        raise ValueError("A quaternion must contain exactly four values")
    components = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in components):
        raise ValueError("Quaternion components must be finite")
    norm = math.sqrt(sum(value * value for value in components))
    if norm < 1e-8:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / norm for value in components)  # type: ignore[return-value]


def normalize_blend_shapes(values: Mapping[str, float]) -> dict[str, float]:
    """Map VMC/VRM0 aliases to VRM expression presets and clamp to [0, 1]."""
    normalized: dict[str, float] = {}
    for raw_name, raw_weight in values.items():
        name = _VMC_EXPRESSION_MAP.get(str(raw_name).strip().lower())
        if name is None:
            continue
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(weight):
            continue
        normalized[name] = max(normalized.get(name, 0.0), min(1.0, max(0.0, weight)))
    return normalized

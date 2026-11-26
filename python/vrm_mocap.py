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

SUPPORTED_VRM_BONES = frozenset(_VMC_BONE_MAP)
SUPPORTED_VRM_BONE_NAMES = frozenset(_VMC_BONE_MAP.values())


def motion_state_to_vrm_state(state: Mapping[str, object]) -> dict[str, dict]:
    """Convert a sampled v1/v2 motion state to normalized VRM bones and expressions."""
    bones: dict[str, dict] = {}

    head_3d = state.get("head_3d")
    if isinstance(head_3d, Mapping):
        try:
            position = tuple(float(value) for value in head_3d["position"])
            rotation = normalize_quaternion(head_3d["rotation"])
            if len(position) == 3 and all(math.isfinite(value) for value in position):
                bones["head"] = {"position": position, "rotation": rotation}
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    else:
        legacy_head = state.get("head", {})
        if isinstance(legacy_head, Mapping):
            try:
                x = float(legacy_head.get("x", 0.0))
                y = float(legacy_head.get("y", 0.0))
                tilt = math.radians(float(legacy_head.get("tilt_deg", 0.0)))
                if all(math.isfinite(value) for value in (x, y, tilt)):
                    bones["head"] = {
                        "position": (x, y, 0.0),
                        "rotation": (0.0, 0.0, math.sin(tilt / 2.0), math.cos(tilt / 2.0)),
                    }
            except (TypeError, ValueError, OverflowError):
                pass

    raw_vrm_bones = state.get("vrm_bones", {})
    if isinstance(raw_vrm_bones, Mapping):
        for source_name, transform in raw_vrm_bones.items():
            name = vmc_bone_to_vrm(str(source_name))
            if name is None or name == "head" or not isinstance(transform, Mapping):
                continue
            try:
                rotation = normalize_quaternion(transform["rotation"])
                normalized = {"rotation": rotation}
                position = transform.get("position")
                if name == "hips" and position is not None:
                    vector = tuple(float(value) for value in position)
                    if len(vector) == 3 and all(math.isfinite(value) for value in vector):
                        normalized["position"] = vector
                bones[name] = normalized
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

    # v1 2D/arm motion remains playable on a VRM model as a compatibility fallback.
    raw_2d_bones = state.get("bones", {})
    if isinstance(raw_2d_bones, Mapping):
        for source_name, raw_rotation in raw_2d_bones.items():
            name = vmc_bone_to_vrm(str(source_name))
            if name is None or name in bones:
                continue
            try:
                bones[name] = {"rotation": normalize_quaternion(raw_rotation)}
            except (TypeError, ValueError, OverflowError):
                continue

    expressions = normalize_blend_shapes(
        state.get("blend_shapes", {}) if isinstance(state.get("blend_shapes", {}), Mapping) else {}
    )
    vowels = state.get("vowels", {})
    if isinstance(vowels, Mapping):
        vowel_aliases = {"A": "aa", "I": "ih", "U": "ou", "E": "ee", "O": "oh"}
        vowel_expressions = normalize_blend_shapes({
            alias: value for vowel, alias in vowel_aliases.items() if (value := vowels.get(vowel)) is not None
        })
        for name, weight in vowel_expressions.items():
            expressions[name] = max(expressions.get(name, 0.0), weight)

    return {"bones": bones, "expressions": expressions}

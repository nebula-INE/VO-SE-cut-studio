from __future__ import annotations

import math

import pytest

from vrm_mocap import (
    motion_state_to_vrm_pose,
    normalize_blend_shapes,
    normalize_quaternion,
    vmc_bone_to_vrm,
)


@pytest.mark.parametrize(
    ("vmc_name", "expected"),
    [
        ("Head", "head"),
        ("Hips", "hips"),
        ("LeftUpperArm", "leftUpperArm"),
        ("RightLowerLeg", "rightLowerLeg"),
        ("LeftIndexIntermediate", "leftIndexIntermediate"),
    ],
)
def test_vmc_bone_mapping(vmc_name: str, expected: str) -> None:
    assert vmc_bone_to_vrm(vmc_name) == expected


def test_unknown_bone_is_ignored() -> None:
    assert vmc_bone_to_vrm("UnknownBone") is None


def test_quaternion_is_normalized() -> None:
    assert normalize_quaternion((0.0, 0.0, 0.0, 2.0)) == (0.0, 0.0, 0.0, 1.0)


def test_zero_quaternion_becomes_identity() -> None:
    assert normalize_quaternion((0.0, 0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0, 1.0)


def test_quaternion_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        normalize_quaternion((0.0, 0.0, math.nan, 1.0))


def test_vmc_blend_shapes_map_to_vrm_presets_and_clamp() -> None:
    result = normalize_blend_shapes({
        "Joy": 1.2,
        "Sorrow": 0.5,
        "A": 0.8,
        "Blink_L": 0.6,
        "Blink_R": 0.4,
        "UnknownCustomExpression": 1.0,
    })
    assert result == {
        "happy": 1.0,
        "sad": 0.5,
        "aa": 0.8,
        "blinkLeft": 0.6,
        "blinkRight": 0.4,
    }


def test_expression_values_ignore_nan_and_keep_max_alias_weight() -> None:
    result = normalize_blend_shapes({"Joy": 0.4, "happy": 0.8, "Angry": math.nan})
    assert result == {"happy": 0.8}



def test_motion_state_to_vrm_pose_converts_legacy_v1_keyframes() -> None:
    pose = motion_state_to_vrm_pose({
        "head": {"x": 0.1, "y": -0.2, "tilt_deg": 30.0},
        "bones": {"LeftUpperArm": (0.0, 0.0, 0.5, 0.8660254)},
        "blend_shapes": {"Joy": 0.8},
        "vowels": {"A": 0.6},
    })
    assert pose["bones"]["head"]["position"] == (0.1, -0.2, 0.0)
    assert pose["bones"]["head"]["rotation"][2] == pytest.approx(0.258819)
    assert pose["bones"]["leftUpperArm"]["rotation"][2] == pytest.approx(0.5)
    assert pose["expressions"]["happy"] == pytest.approx(0.8)
    assert pose["expressions"]["aa"] == pytest.approx(0.6)


def test_motion_state_to_vrm_pose_prefers_v2_humanoid_keyframes() -> None:
    pose = motion_state_to_vrm_pose({
        "vrm": {
            "head": {"position": (0.0, 1.0, 0.0), "rotation": (0.0, 0.0, 0.0, 1.0)},
            "bones": {"hips": {"position": (0.2, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0, 1.0)}},
        },
        "head": {"x": 9.0, "y": 9.0, "tilt_deg": 90.0},
        "bones": {},
        "blend_shapes": {},
        "vowels": {},
    })
    assert pose["bones"]["head"]["position"] == (0.0, 1.0, 0.0)
    assert pose["bones"]["hips"]["position"] == (0.2, 0.0, 0.0)



def test_canonical_vrm_blink_presets_survive_motion_normalization() -> None:
    result = normalize_blend_shapes({
        "blinkLeft": 0.7,
        "blinkRight": 0.4,
    })
    assert result == {"blinkLeft": 0.7, "blinkRight": 0.4}

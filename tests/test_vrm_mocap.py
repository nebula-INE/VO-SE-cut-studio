from __future__ import annotations

import math

import pytest

from vrm_mocap import normalize_blend_shapes, normalize_quaternion, vmc_bone_to_vrm


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

def test_v1_v2_motion_state_maps_to_normalized_vrm_pose():
    from vrm_mocap import motion_state_to_vrm_state

    pose = motion_state_to_vrm_state({
        "head_3d": {
            "position": [0.1, 0.2, 0.3],
            "rotation": [0.0, 0.0, 0.0, 2.0],
        },
        "vrm_bones": {
            "Hips": {
                "position": [0.4, 0.5, 0.6],
                "rotation": [0.0, 0.0, 0.0, 1.0],
            },
            "LeftUpperArm": {
                "position": [9.0, 9.0, 9.0],
                "rotation": [0.0, 0.0, 0.5, 0.8660254],
            },
        },
        "blend_shapes": {"Joy": 0.8, "Blink_L": 0.7, "Blink_R": 0.2},
        "vowels": {"A": 0.6},
    })

    assert pose["bones"]["head"]["position"] == (0.1, 0.2, 0.3)
    assert pose["bones"]["head"]["rotation"] == (0.0, 0.0, 0.0, 1.0)
    assert pose["bones"]["hips"]["position"] == (0.4, 0.5, 0.6)
    assert "position" not in pose["bones"]["leftUpperArm"]
    assert pose["expressions"] == {
        "happy": 0.8,
        "blinkLeft": 0.7,
        "blinkRight": 0.2,
        "aa": 0.6,
    }


def test_legacy_motion_pose_maps_2d_head_and_arm_quaternion():
    from vrm_mocap import motion_state_to_vrm_state

    pose = motion_state_to_vrm_state({
        "head": {"x": 0.1, "y": -0.2, "tilt_deg": 90.0},
        "bones": {"LeftUpperArm": [0.0, 0.0, 0.0, 1.0]},
        "blend_shapes": {"Joy": 0.5},
        "vowels": {},
    })

    assert pose["bones"]["head"]["position"] == (0.1, -0.2, 0.0)
    assert pose["bones"]["head"]["rotation"][2] == pytest.approx(math.sqrt(0.5))
    assert "leftUpperArm" in pose["bones"]
    assert pose["expressions"]["happy"] == pytest.approx(0.5)


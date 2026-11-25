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

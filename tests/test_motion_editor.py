import math

import pytest

from character_motion import (
    CharacterMotionError,
    normalize_motion_keyframes,
    sample_motion,
)
from motion_editor import (
    delete_motion_keyframe,
    insert_motion_keyframe,
    quaternion_roll_deg,
    roll_quaternion,
)


def _frames():
    return [
        {
            "time": 0.0,
            "mouth_openness": 0.0,
            "head": {"x": 0.0, "y": 0.0, "tilt_deg": 0.0},
            "blend_shapes": {"Joy": 0.0},
            "vowels": {"A": 0.0},
            "bones": {"LeftUpperArm": [0.0, 0.0, 0.0, 1.0]},
        },
        {
            "time": 1.0,
            "mouth_openness": 1.0,
            "head": {"x": 1.0, "y": -0.5, "tilt_deg": 30.0},
            "blend_shapes": {"Joy": 1.0, "Blink": 0.5},
            "vowels": {"A": 1.0},
            "bones": {
                "LeftUpperArm": [
                    0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)
                ]
            },
        },
    ]


def test_insert_keyframe_uses_interpolated_state_and_keeps_order():
    frames, index = insert_motion_keyframe(_frames(), 0.25)

    assert index == 1
    assert [frame["time"] for frame in frames] == [0.0, 0.25, 1.0]
    state = sample_motion(frames, 0.25)
    assert state["head"]["x"] == pytest.approx(0.25)
    assert state["head"]["y"] == pytest.approx(-0.125)
    assert state["head"]["tilt_deg"] == pytest.approx(7.5)
    assert state["mouth_openness"] == pytest.approx(0.25)
    assert state["blend_shapes"]["Joy"] == pytest.approx(0.25)
    assert state["vowels"]["A"] == pytest.approx(0.25)


def test_insert_after_last_keyframe_copies_the_last_pose():
    frames, index = insert_motion_keyframe(_frames(), 2.0)

    assert index == 2
    assert frames[-1]["time"] == pytest.approx(2.0)
    assert frames[-1]["head"]["x"] == pytest.approx(1.0)
    assert frames[-1]["mouth_openness"] == pytest.approx(1.0)


def test_insert_near_existing_keyframe_selects_it_without_duplication():
    frames, index = insert_motion_keyframe(_frames(), 1.0 + 1e-7)

    assert index == 1
    assert len(frames) == 2


@pytest.mark.parametrize("timestamp", [-1.0, float("nan"), float("inf"), True])
def test_insert_rejects_invalid_timestamps(timestamp):
    with pytest.raises(CharacterMotionError, match="time"):
        insert_motion_keyframe(_frames(), timestamp)


def test_delete_keyframe_keeps_other_frames_and_rejects_last_frame():
    frames = delete_motion_keyframe(_frames(), 0)

    assert len(frames) == 1
    assert frames[0]["time"] == pytest.approx(1.0)
    with pytest.raises(CharacterMotionError, match="at least one"):
        delete_motion_keyframe(frames, 0)


def test_delete_rejects_out_of_range_index():
    with pytest.raises(CharacterMotionError, match="out of range"):
        delete_motion_keyframe(_frames(), 2)


@pytest.mark.parametrize("angle", [-179.0, -90.0, 0.0, 45.0, 179.0])
def test_arm_roll_quaternion_round_trip(angle):
    quaternion = roll_quaternion(angle)
    assert sum(value * value for value in quaternion) == pytest.approx(1.0)
    assert quaternion_roll_deg(tuple(quaternion)) == pytest.approx(angle)


def test_normalize_motion_keyframes_keeps_full_float_precision():
    original_time = 0.123456789123
    original_x = 0.0123456789123
    frames = normalize_motion_keyframes([
        {
            "time": original_time,
            "head": {"x": original_x},
            "bones": {
                "RightUpperArm": [
                    0.123456789, 0.234567891, 0.345678912, 0.456789123
                ]
            },
        }
    ])

    assert frames[0]["time"] == original_time
    assert frames[0]["head"]["x"] == original_x
    quaternion = frames[0]["bones"]["RightUpperArm"]
    assert quaternion[0] == pytest.approx(0.123456789 / math.sqrt(
        0.123456789**2 + 0.234567891**2 + 0.345678912**2 + 0.456789123**2
    ))

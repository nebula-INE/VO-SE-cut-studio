import json
import math

import pytest

from character_motion import CharacterMotionError, load_motion_file, sample_motion


def _write_motion(path, keyframes):
    path.write_text(
        json.dumps({
            "format": "aural-motion",
            "version": 1,
            "keyframes": keyframes,
        }),
        encoding="utf-8",
    )
    return path


def test_load_motion_file_sorts_and_interpolates_keyframes(tmp_path):
    path = _write_motion(tmp_path / "actor.motion.json", [
        {"time": 1.0, "mouth_openness": 1.0, "head": {"tilt_deg": 20.0}},
        {"time": 0.0, "mouth_openness": 0.0, "head": {"tilt_deg": 0.0}},
    ])

    frames = load_motion_file(path)
    state = sample_motion(frames, 0.5)

    assert [frame["time"] for frame in frames] == [0.0, 1.0]
    assert state["mouth_openness"] == pytest.approx(0.5)
    assert state["head"]["tilt_deg"] == pytest.approx(10.0)


def test_motion_weights_and_quaternions_are_interpolated(tmp_path):
    path = _write_motion(tmp_path / "actor.motion.json", [
        {
            "time": 0.0,
            "blend_shapes": {"Joy": 0.0},
            "vowels": {"A": 0.0},
            "bones": {"LeftUpperArm": [0.0, 0.0, 0.0, 1.0]},
        },
        {
            "time": 1.0,
            "blend_shapes": {"Joy": 1.0},
            "vowels": {"A": 1.0},
            "bones": {
                "LeftUpperArm": [
                    0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)
                ]
            },
        },
    ])

    state = sample_motion(load_motion_file(path), 0.5)
    assert state["blend_shapes"]["Joy"] == pytest.approx(0.5)
    assert state["vowels"]["A"] == pytest.approx(0.5)
    quaternion = state["bones"]["LeftUpperArm"]
    assert sum(value * value for value in quaternion) == pytest.approx(1.0)
    assert quaternion[2] > 0.0


@pytest.mark.parametrize("payload", [
    {"format": "other", "version": 1, "keyframes": [{"time": 0}]},
    {"format": "aural-motion", "version": 2, "keyframes": [{"time": 0}]},
    {"format": "aural-motion", "version": 1, "keyframes": []},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": -1}]},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": 0}, {"time": 0}]},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": 0, "vowels": {"X": 1}}]},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": 0, "bones": {"Spine": [0, 0, 0, 1]}}]},
])
def test_load_motion_file_rejects_invalid_documents(tmp_path, payload):
    path = tmp_path / "bad.motion.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CharacterMotionError):
        load_motion_file(path)


def test_motion_file_size_limit_is_enforced(tmp_path):
    path = tmp_path / "large.motion.json"
    path.write_text(" " * (10 * 1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(CharacterMotionError, match="too large"):
        load_motion_file(path)

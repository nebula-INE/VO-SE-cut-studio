import json
import math

import pytest

from character_motion import CharacterMotionError, load_motion_file, sample_motion, save_motion_file


def _write_motion(path, keyframes, version=1):
    path.write_text(
        json.dumps({
            "format": "aural-motion",
            "version": version,
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
    {"format": "aural-motion", "version": 9, "keyframes": [{"time": 0}]},
    {"format": "aural-motion", "version": 1, "keyframes": []},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": -1}]},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": 0}, {"time": 0}]},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": 0, "vowels": {"X": 1}}]},
    {"format": "aural-motion", "version": 1, "keyframes": [{"time": 0, "bones": {"Spine": [0, 0, 0, 1]}}]},
    {"format": "aural-motion", "version": 2, "keyframes": [{"time": 0, "vrm": {"bones": {"madeUp": {"rotation": [0, 0, 0, 1]}}}}]},
    {"format": "aural-motion", "version": 2, "keyframes": [{"time": 0, "vrm": {"head": {"position": [0, 1]}}}]},
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


def test_save_motion_file_round_trips_legacy_motion_as_v1(tmp_path):
    path = save_motion_file(
        tmp_path / "recorded.motion.json",
        [
            {
                "time": 0.0,
                "head": {"x": 0.0, "y": 0.0, "tilt_deg": 0.0},
                "bones": {"LeftUpperArm": [0.0, 0.0, 0.0, 1.0]},
            },
            {
                "time": 0.5,
                "head": {"x": 0.1, "y": 0.0, "tilt_deg": 12.0},
                "vowels": {"A": 0.8},
                "bones": {"LeftUpperArm": [0.0, 0.0, 0.5, 0.8660254]},
            },
        ],
    )

    raw = path.read_bytes()
    payload = json.loads(raw)
    assert raw.endswith(b"\n")
    assert payload["version"] == 1
    assert load_motion_file(path)[-1]["head"]["tilt_deg"] == pytest.approx(12.0)
    assert not list(tmp_path.glob("*.tmp"))


def test_vrm_motion_round_trip_and_slerp(tmp_path):
    start_rotation = [0.0, 0.0, 0.0, 1.0]
    end_rotation = [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]
    frames = [
        {
            "time": 0.0,
            "vrm": {
                "head": {"position": [0.0, 1.0, 0.0], "rotation": start_rotation},
                "bones": {"hips": {"position": [0.0, 0.0, 0.0], "rotation": start_rotation}},
            },
        },
        {
            "time": 1.0,
            "vrm": {
                "head": {"position": [1.0, 2.0, 0.0], "rotation": end_rotation},
                "bones": {"hips": {"position": [0.2, 0.0, 0.0], "rotation": end_rotation}},
            },
        },
    ]
    path = save_motion_file(tmp_path / "vrm-take.motion.json", frames)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    loaded = load_motion_file(path)
    state = sample_motion(loaded, 0.5)

    assert state["vrm"]["head"]["position"] == pytest.approx((0.5, 1.5, 0.0))
    rotation = state["vrm"]["head"]["rotation"]
    assert sum(value * value for value in rotation) == pytest.approx(1.0)
    assert rotation[2] > 0.0
    assert state["vrm"]["bones"]["hips"]["position"] == pytest.approx((0.1, 0.0, 0.0))


def test_v1_motion_without_vrm_remains_loadable(tmp_path):
    path = _write_motion(tmp_path / "old.motion.json", [
        {"time": 0.0, "head": {"x": 0.0, "y": 0.0, "tilt_deg": 0.0}},
    ], version=1)
    assert load_motion_file(path)[0]["time"] == 0.0



def test_first_vrm_pose_interpolates_from_neutral_transform(tmp_path):
    path = _write_motion(tmp_path / "first-pose.motion.json", [
        {"time": 0.0},
        {
            "time": 1.0,
            "vrm": {
                "head": {
                    "position": [1.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)],
                }
            },
        },
    ])

    state = sample_motion(load_motion_file(path), 0.5)
    assert state["vrm"]["head"]["position"] == pytest.approx((0.5, 0.0, 0.0))
    assert state["vrm"]["head"]["rotation"][2] == pytest.approx(math.sin(math.pi / 8))

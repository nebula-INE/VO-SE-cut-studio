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
    {"format": "aural-motion", "version": 3, "keyframes": [{"time": 0}]},
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
    path.write_text(
        json.dumps({
            "format": "aural-motion",
            "version": 1,
            "keyframes": [{"time": 0.0, "ignored_large_metadata": "x" * (10 * 1024 * 1024 + 1)}],
        }),
        encoding="utf-8",
    )
    with pytest.raises(CharacterMotionError, match="too large"):
        load_motion_file(path)


def test_save_motion_file_round_trips_and_adds_a_terminal_newline(tmp_path):
    from character_motion import save_motion_file

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
    assert raw.endswith(b"\n")
    assert json.loads(raw)["format"] == "aural-motion"
    assert load_motion_file(path)[-1]["head"]["tilt_deg"] == pytest.approx(12.0)
    assert not list(tmp_path.glob("*.tmp"))


def test_save_motion_file_rejects_duplicate_times_and_invalid_values(tmp_path):
    from character_motion import save_motion_file

    with pytest.raises(CharacterMotionError, match="unique"):
        save_motion_file(
            tmp_path / "duplicate.motion.json",
            [{"time": 0.0}, {"time": 0.0}],
        )
    with pytest.raises(CharacterMotionError, match="zero quaternion"):
        save_motion_file(
            tmp_path / "invalid.motion.json",
            [{"time": 0.0, "bones": {"LeftUpperArm": [0.0, 0.0, 0.0, 0.0]}}],
        )

def test_vrm_motion_v2_round_trips_and_interpolates_full_body_pose(tmp_path):
    from character_motion import save_motion_file

    path = save_motion_file(
        tmp_path / "vrm-take.motion.json",
        [
            {
                "time": 0.0,
                "head_3d": {
                    "position": [0.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, 0.0, 1.0],
                },
                "vrm_bones": {
                    "Hips": {
                        "position": [0.0, 0.0, 0.0],
                        "rotation": [0.0, 0.0, 0.0, 1.0],
                    },
                    "LeftUpperArm": {
                        "position": [0.0, 0.1, 0.0],
                        "rotation": [0.0, 0.0, 0.0, 1.0],
                    },
                },
            },
            {
                "time": 1.0,
                "head_3d": {
                    "position": [0.2, 0.0, 0.0],
                    "rotation": [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)],
                },
                "vrm_bones": {
                    "Hips": {
                        "position": [0.0, 0.4, 0.0],
                        "rotation": [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)],
                    },
                    "LeftUpperArm": {
                        "position": [0.0, 0.1, 0.0],
                        "rotation": [0.0, math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4)],
                    },
                },
                "blend_shapes": {"Joy": 0.8},
            },
        ],
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    state = sample_motion(load_motion_file(path), 0.5)
    assert state["head_3d"]["position"][0] == pytest.approx(0.1)
    assert state["vrm_bones"]["Hips"]["position"][1] == pytest.approx(0.2)
    rotation = state["head_3d"]["rotation"]
    assert sum(value * value for value in rotation) == pytest.approx(1.0)
    assert state["blend_shapes"]["Joy"] == pytest.approx(0.4)


def test_motion_v1_rejects_vrm_fields_and_v2_rejects_unknown_bones(tmp_path):
    from character_motion import save_motion_file

    with pytest.raises(CharacterMotionError, match="version 1"):
        save_motion_file(
            tmp_path / "bad-v1.motion.json",
            [{"time": 0.0, "head_3d": {"position": [0, 0, 0], "rotation": [0, 0, 0, 1]}}],
        )

    path = tmp_path / "bad-v2.motion.json"
    path.write_text(json.dumps({
        "format": "aural-motion",
        "version": 2,
        "keyframes": [{"time": 0.0, "vrm_bones": {"NotABone": {"rotation": [0, 0, 0, 1]}}}],
    }), encoding="utf-8")
    with pytest.raises(CharacterMotionError, match="Unsupported VRM bone"):
        load_motion_file(path)

def test_vrm_v2_rejects_malformed_positions_and_quaternions(tmp_path):
    payloads = [
        {
            "head_3d": {
                "position": [0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0, 1.0],
            }
        },
        {
            "head_3d": {
                "position": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0, 0.0],
            }
        },
        {
            "vrm_bones": {
                "Hips": {
                    "position": [0.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, float("nan"), 1.0],
                }
            }
        },
    ]
    for index, fields in enumerate(payloads):
        path = tmp_path / f"bad-vrm-{index}.motion.json"
        path.write_text(json.dumps({
            "format": "aural-motion",
            "version": 2,
            "keyframes": [{"time": 0.0, **fields}],
        }), encoding="utf-8")
        with pytest.raises(CharacterMotionError):
            load_motion_file(path)


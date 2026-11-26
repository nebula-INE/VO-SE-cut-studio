import pytest

from character_motion import load_motion_file, save_motion_file
from motion_capture_recorder import (
    MAX_RECORDING_SECONDS,
    MotionCaptureRecorder,
    MotionCaptureRecordingError,
)


def test_recorder_captures_head_expression_vowels_and_supported_bones():
    now = [100.0]
    recorder = MotionCaptureRecorder(clock=lambda: now[0])
    recorder.start()

    recorder.update_head(0.05, -0.02, 15.0)
    recorder.update_blend_shapes({
        "happy": 0.8,
        "aa": 0.7,
        "Blink_L": 1.0,
        "unmapped-source-shape": 1.0,
    })
    recorder.update_bone("LeftUpperArm", 0.0, 0.0, 0.5, 0.8660254)
    recorder.update_bone("Spine", 0.0, 0.0, 0.0, 1.0)

    now[0] += 1.0 / 30.0
    assert recorder.capture()
    now[0] += 1.0 / 30.0
    recorder.update_head(0.1, 0.0, 25.0)
    assert recorder.capture()

    now[0] += 0.5
    frames = recorder.stop()

    assert not recorder.is_recording
    assert frames[1]["head"] == {"x": 0.05, "y": -0.02, "tilt_deg": 15.0}
    assert frames[1]["blend_shapes"]["Joy"] == pytest.approx(0.8)
    assert frames[1]["vowels"]["A"] == pytest.approx(0.7)
    assert frames[1]["blend_shapes"]["Blink_L"] == pytest.approx(1.0)
    assert "unmapped-source-shape" not in frames[1]["blend_shapes"]
    assert "Spine" not in frames[1]["bones"]
    quaternion = frames[1]["bones"]["LeftUpperArm"]
    assert sum(value * value for value in quaternion) == pytest.approx(1.0)
    assert frames[-1]["time"] >= 0.5


def test_recorder_rate_limits_capture_and_elides_unchanged_snapshots():
    now = [1.0]
    recorder = MotionCaptureRecorder(clock=lambda: now[0])
    recorder.start()
    recorder.update_head(0.2, 0.0, 0.0)

    now[0] += 0.01
    assert not recorder.capture()
    now[0] += 0.03
    assert recorder.capture()
    count = recorder.keyframe_count

    now[0] += 0.04
    assert not recorder.capture()
    assert recorder.keyframe_count == count

    now[0] += 0.2
    frames = recorder.stop()
    assert frames[-1]["time"] >= 0.2


def test_recorder_short_capture_gets_a_minimum_nonzero_duration():
    now = [5.0]
    recorder = MotionCaptureRecorder(clock=lambda: now[0])
    recorder.start()
    now[0] += 0.001
    frames = recorder.stop()

    assert len(frames) == 2
    assert frames[-1]["time"] == pytest.approx(0.05)


def test_recorder_rejects_overlapping_start_and_stopping_when_idle():
    recorder = MotionCaptureRecorder(clock=lambda: 1.0)
    with pytest.raises(MotionCaptureRecordingError, match="No recording"):
        recorder.stop()
    recorder.start()
    with pytest.raises(MotionCaptureRecordingError, match="already active"):
        recorder.start()
    recorder.cancel()
    assert not recorder.is_recording


def test_recorder_enforces_maximum_duration():
    now = [0.0]
    recorder = MotionCaptureRecorder(clock=lambda: now[0])
    recorder.start()
    now[0] = MAX_RECORDING_SECONDS + 1.0
    with pytest.raises(MotionCaptureRecordingError, match="Maximum recording"):
        recorder.capture()
    frames = recorder.stop()
    assert frames[-1]["time"] == pytest.approx(MAX_RECORDING_SECONDS)


def test_recorded_keyframes_can_be_saved_and_reloaded(tmp_path):
    now = [20.0]
    recorder = MotionCaptureRecorder(clock=lambda: now[0])
    recorder.start()
    recorder.update_head(0.1, 0.0, -5.0)
    now[0] += 0.1
    recorder.capture()
    now[0] += 0.25
    frames = recorder.stop()

    path = save_motion_file(tmp_path / "live-take.motion.json", frames)
    loaded = load_motion_file(path)

    assert len(loaded) >= 3
    assert loaded[1]["head"]["x"] == pytest.approx(0.1)
    assert loaded[-1]["time"] >= 0.35


def test_recorder_captures_vrm_head_and_full_body_positions(tmp_path):
    now = [50.0]
    recorder = MotionCaptureRecorder(clock=lambda: now[0])
    recorder.start()

    recorder.update_vrm_head(
        0.1, 0.2, 0.3, 0.0, 0.0, 0.70710678, 0.70710678
    )
    recorder.update_vrm_bone(
        "Hips", 0.4, 0.5, 0.6, 0.0, 0.0, 0.0, 1.0
    )
    recorder.update_vrm_bone(
        "LeftUpperArm", 1.0, 2.0, 3.0, 0.0, 0.38268343, 0.0, 0.92387953
    )
    recorder.update_vrm_bone(
        "UnknownBone", 0, 0, 0, 0, 0, 0, 1
    )

    now[0] += 1.0 / 30.0
    assert recorder.capture()
    now[0] += 0.2
    frames = recorder.stop()

    path = save_motion_file(tmp_path / "vrm-take.motion.json", frames)
    loaded = load_motion_file(path)
    assert loaded[1]["head_3d"]["position"] == pytest.approx([0.1, 0.2, 0.3])
    assert loaded[1]["vrm_bones"]["Hips"]["position"] == pytest.approx([0.4, 0.5, 0.6])
    assert "UnknownBone" not in loaded[1]["vrm_bones"]


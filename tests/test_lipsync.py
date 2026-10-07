import wave

import numpy as np
import pytest

import lipsync
from synth_vowel import SAMPLE_RATE, synth_vowel, write_wav


@pytest.mark.parametrize("vowel", list("AIUEO"))
def test_synthetic_vowel_is_classified(tmp_path, vowel):
    path = str(tmp_path / "v.wav")
    write_wav(path, synth_vowel(vowel))
    frames = lipsync.extract_lipsync_frames(path, noise_floor=0.0)
    mid = frames[len(frames) // 2]
    assert mid.mouth_openness > 0.5
    assert max(mid.vowels, key=mid.vowels.get) == vowel


def test_silence_has_no_vowels_and_closed_mouth(tmp_path):
    path = str(tmp_path / "silence.wav")
    write_wav(path, np.zeros(SAMPLE_RATE // 2))
    frames = lipsync.extract_lipsync_frames(path)
    assert all(f.mouth_openness == 0.0 and f.vowels == {} for f in frames)


def test_vowel_weights_never_exceed_openness(tmp_path):
    path = str(tmp_path / "v.wav")
    write_wav(path, synth_vowel("A"))
    for f in lipsync.extract_lipsync_frames(path, noise_floor=0.0):
        assert sum(f.vowels.values()) <= f.mouth_openness + 1e-6


def test_volume_only_api_is_unchanged(tmp_path):
    path = str(tmp_path / "v.wav")
    write_wav(path, synth_vowel("A"))
    frames = lipsync.extract_mouth_envelope(path)
    assert frames and all(f.vowels == {} for f in frames)  # 母音は付かない(後方互換)


def test_vowels_at_nearest_frame():
    frames = [
        lipsync.LipSyncFrame(0.0, 0.0),
        lipsync.LipSyncFrame(0.1, 0.9, {"A": 0.9}),
    ]
    assert lipsync.vowels_at(frames, 0.11) == {"A": 0.9}
    assert lipsync.vowels_at([], 0.0) == {}


def test_classify_vowel_sums_to_one():
    weights = lipsync.classify_vowel(300.0, 2300.0)
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert max(weights, key=weights.get) == "I"

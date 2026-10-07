"""テスト用: 声門パルス列をフォルマント共鳴器に通して、母音らしい合成音声(WAV)を作る。"""

import wave

import numpy as np

SAMPLE_RATE = 22050

# テスト用の母音ごとのF1/F2/F3/F4(Hz)。lipsync.VOWEL_FORMANTSとは別に持つ(代表値そのものを
# 使うと「自分で作った値で自分を当てる」だけのテストになるため、やや異なる値にしている)。
TEST_FORMANTS = {
    "A": (750, 1200, 2600, 3400),
    "I": (320, 2200, 2900, 3500),
    "U": (380, 1250, 2400, 3400),
    "E": (520, 1850, 2600, 3400),
    "O": (480, 800, 2500, 3400),
}


def _resonator(x: np.ndarray, freq: float, bw: float) -> np.ndarray:
    r = np.exp(-np.pi * bw / SAMPLE_RATE)
    theta = 2.0 * np.pi * freq / SAMPLE_RATE
    a1, a2 = -2.0 * r * np.cos(theta), r * r
    gain = 1.0 - r
    y = np.zeros_like(x)
    for n in range(len(x)):
        y1 = y[n - 1] if n >= 1 else 0.0
        y2 = y[n - 2] if n >= 2 else 0.0
        y[n] = gain * x[n] - a1 * y1 - a2 * y2
    return y


def synth_vowel(vowel: str, duration_sec: float = 0.5, f0: float = 120.0) -> np.ndarray:
    n = int(SAMPLE_RATE * duration_sec)
    source = np.zeros(n)
    source[:: int(SAMPLE_RATE / f0)] = 1.0
    signal = source
    for freq in TEST_FORMANTS[vowel]:
        signal = _resonator(signal, freq, bw=80.0)
    return signal / (np.max(np.abs(signal)) + 1e-9) * 0.6


def write_wav(path: str, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())

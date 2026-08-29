"""aural Studio - 自動リップシンク(基礎)

音声波形の振幅(音量)から、フレームごとの「口の開き具合」を算出する。
音素単位の精密なリップシンクではなく、plan.mdの「音声波形・周波数に合わせて
自動で口パク・微動する」という基礎機能の要求通り、音量ベースの簡易的な
リップシンクとして実装している。

処理の流れ:
    WAVファイル
      → フレーム(動画fps基準)ごとのRMS音量を算出
      → 無音区間の底上げノイズを除去(ノイズフロア以下は0扱い)
      → 0〜1に正規化
      → アタック/リリースで軽くスムージング(音量の急変で口がガクガクしない
        ように、開くのは速く・閉じるのはやや遅く追従させる)
"""

from __future__ import annotations

import wave
from dataclasses import dataclass

import numpy as np


@dataclass
class LipSyncFrame:
    time_sec: float
    mouth_openness: float  # 0.0(閉じる)〜1.0(全開)


def _read_wav_as_float(wav_path: str) -> tuple[np.ndarray, int]:
    """WAVファイルを読み込み、-1.0〜1.0のfloat32配列(モノラル)として返す。"""
    with wave.open(wav_path, "rb") as w:
        sample_rate = w.getframerate()
        n_channels = w.getnchannels()
        sample_width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if sample_width != 2:
        raise ValueError(f"16bit PCM以外のWAVには未対応です(sampwidth={sample_width})")

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    return samples, sample_rate


def extract_mouth_envelope(
    wav_path: str,
    fps: float = 30.0,
    noise_floor: float = 0.02,
    attack: float = 0.6,
    release: float = 0.25,
) -> list[LipSyncFrame]:
    """WAVファイルから、動画fps基準でのフレームごとの口の開き具合を算出する。

    Args:
        wav_path: 入力WAVファイル(16bit PCM)。
        fps: 動画のフレームレート。この間隔でエンベロープをサンプリングする。
        noise_floor: これ未満のRMS音量は無音(口を閉じる)とみなす閾値(0〜1)。
        attack: 音量が大きくなる方向への追従の速さ(0〜1、大きいほど速い)。
            人の口の開閉のうち「開く」動作は比較的素早いため、releaseより
            大きい値をデフォルトにしている。
        release: 音量が小さくなる方向への追従の速さ(0〜1、大きいほど速い)。
            「閉じる」動作は開くより緩やかに見えることが多いため、attackより
            小さい値をデフォルトにしている。

    Returns:
        LipSyncFrameのリスト(時刻順)。
    """
    samples, sample_rate = _read_wav_as_float(wav_path)
    duration_sec = len(samples) / sample_rate
    frame_interval = 1.0 / fps
    frame_count = max(1, int(duration_sec / frame_interval) + 1)

    # フレームごとのウィンドウ幅(RMS算出に使う区間)。fpsの間隔そのものだと
    # 短すぎて瞬間的なノイズを拾いやすいため、やや広めに取る。
    window_sec = frame_interval * 1.5
    window_samples = max(1, int(window_sec * sample_rate))

    raw_levels: list[float] = []
    for i in range(frame_count):
        center_sec = i * frame_interval
        center_sample = int(center_sec * sample_rate)
        start = max(0, center_sample - window_samples // 2)
        end = min(len(samples), start + window_samples)
        if start >= end:
            raw_levels.append(0.0)
            continue
        window = samples[start:end]
        rms = float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))
        raw_levels.append(rms)

    # ノイズフロア除去 + 正規化(音声全体の最大音量を1.0とする)
    max_level = max(raw_levels) if raw_levels else 0.0
    if max_level < 1e-6:
        normalized = [0.0] * len(raw_levels)
    else:
        normalized = []
        for level in raw_levels:
            level = max(0.0, level - noise_floor)
            normalized.append(min(1.0, level / max(max_level - noise_floor, 1e-6)))

    # アタック/リリースでスムージング(フレーム間の急変を抑える)
    frames: list[LipSyncFrame] = []
    smoothed = 0.0
    for i, target in enumerate(normalized):
        rate = attack if target > smoothed else release
        smoothed += (target - smoothed) * rate
        frames.append(LipSyncFrame(time_sec=i * frame_interval, mouth_openness=smoothed))

    return frames


def mouth_openness_at(frames: list[LipSyncFrame], time_sec: float) -> float:
    """任意の時刻に最も近いフレームの口の開き具合を返す(簡易的な最近傍検索)。"""
    if not frames:
        return 0.0
    fps_interval = frames[1].time_sec - frames[0].time_sec if len(frames) > 1 else 1.0
    index = int(round(time_sec / fps_interval)) if fps_interval > 0 else 0
    index = max(0, min(len(frames) - 1, index))
    return frames[index].mouth_openness


if __name__ == "__main__":
    import sys

    wav_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/openjtalk_test.wav"
    frames = extract_mouth_envelope(wav_path)

    print(f"入力: {wav_path}")
    print(f"フレーム数: {len(frames)}")
    print()
    for f in frames[:60]:  # 先頭2秒分だけ表示
        bar = "#" * int(f.mouth_openness * 30)
        print(f"{f.time_sec:6.3f}s [{bar:<30}] {f.mouth_openness:.2f}")

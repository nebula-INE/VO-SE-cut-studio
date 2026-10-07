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
from dataclasses import dataclass, field

import numpy as np


@dataclass
class LipSyncFrame:
    time_sec: float
    mouth_openness: float  # 0.0(閉じる)〜1.0(全開)
    # 母音ごとの重み(A/I/U/E/Oの合計が1.0以下)。extract_lipsync_frames()でのみ
    # 埋まる。extract_mouth_envelope()(音量のみ)の結果では空dict。
    vowels: dict[str, float] = field(default_factory=dict)


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



# ---------------------------------------------------------------------------
# 母音推定(フォルマントによる簡易ビゼーム)
# ---------------------------------------------------------------------------
#
# 音量だけでなく声の周波数特性(第1・第2フォルマント F1/F2)から、日本語5母音
# (あいうえお)のどれに近い口形かを推定する。各フレームをLPC(線形予測)で
# 分析し、スペクトル包絡のピークからF1/F2を求め、母音ごとの代表値との
# 距離でソフトに重み付けする。
#
# 精度の注意: 話者・声域(特に高い声や歌声)によってフォルマントは大きく
# ずれるため、代表値は成人の話し声の目安。誤認識しても口が破綻しないよう、
# 母音の重みは「音量(mouth_openness)」と「有声らしさ」で必ず減衰させる。

# 日本語5母音の代表的なF1/F2(Hz)。成人話者の平均的な目安値。
VOWEL_FORMANTS: dict[str, tuple[float, float]] = {
    "A": (800.0, 1250.0),
    "I": (300.0, 2300.0),
    "U": (350.0, 1300.0),
    "E": (500.0, 1900.0),
    "O": (500.0, 850.0),
}

_ANALYSIS_RATE = 11025  # フォルマント分析用にこの付近までダウンサンプルする
_LPC_ORDER = 12
_VOWEL_SOFTNESS = 0.35  # 小さいほど最も近い母音に重みが集中する


def _decimate(samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, int]:
    """整数倍の間引きで分析用レートへ落とす(間引き前に移動平均で簡易LPF)。"""
    factor = max(1, int(round(sample_rate / _ANALYSIS_RATE)))
    if factor == 1:
        return samples, sample_rate
    kernel = np.ones(factor, dtype=np.float64) / factor
    filtered = np.convolve(samples.astype(np.float64), kernel, mode="same")
    return filtered[::factor], sample_rate // factor


def _lpc(frame: np.ndarray, order: int) -> np.ndarray | None:
    """自己相関法+Levinson-Durbinで線形予測係数(先頭1.0)を求める。"""
    n = len(frame)
    if n <= order:
        return None
    r = np.correlate(frame, frame, mode="full")[n - 1:n + order]
    if r[0] < 1e-9:
        return None
    r = r.copy()
    r[0] *= 1.0 + 1e-6  # 数値安定化(ホワイトノイズ補正)
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1])
        k = -acc / err
        prev = a[1:i].copy()
        a[1:i] = prev + k * prev[::-1]
        a[i] = k
        err *= 1.0 - k * k
        if err <= 1e-12:
            break
    return a


def estimate_formants(frame: np.ndarray, sample_rate: int) -> tuple[float, float] | None:
    """1フレームのF1,F2(Hz)を推定する。求まらなければNone。"""
    if len(frame) < _LPC_ORDER * 2:
        return None
    emphasized = np.append(frame[0], frame[1:] - 0.97 * frame[:-1])  # 高域強調
    windowed = emphasized * np.hanning(len(emphasized))
    a = _lpc(windowed, _LPC_ORDER)
    if a is None:
        return None
    roots = np.roots(a)
    roots = roots[np.imag(roots) > 0.01]  # 共役の片側のみ
    if len(roots) == 0:
        return None
    freqs = np.angle(roots) * sample_rate / (2.0 * np.pi)
    bandwidths = -(sample_rate / np.pi) * np.log(np.abs(roots))
    candidates = sorted(
        f for f, bw in zip(freqs, bandwidths) if 200.0 < f < 3500.0 and bw < 400.0
    )
    if len(candidates) < 2:
        return None
    return float(candidates[0]), float(candidates[1])


def classify_vowel(f1: float, f2: float) -> dict[str, float]:
    """(F1,F2)から母音ごとの重み(合計1.0)をソフトに求める。"""
    scores: dict[str, float] = {}
    for vowel, (t1, t2) in VOWEL_FORMANTS.items():
        # 対数周波数上の距離(人間の知覚に近い尺度)
        d = np.hypot(np.log(f1 / t1), np.log(f2 / t2))
        scores[vowel] = float(np.exp(-d / _VOWEL_SOFTNESS))
    total = sum(scores.values())
    if total <= 0.0:
        return {}
    return {v: s / total for v, s in scores.items()}


def extract_lipsync_frames(wav_path: str, fps: float = 30.0, **envelope_kwargs) -> list[LipSyncFrame]:
    """音量エンベロープ(extract_mouth_envelope)に、母音の重みを付加して返す。

    envelope_kwargsはextract_mouth_envelopeにそのまま渡される(noise_floor等)。
    母音の重みは mouth_openness を掛けて減衰させる(無音・小声では母音形状に
    ならず口が閉じる方向へ寄る)。
    """
    frames = extract_mouth_envelope(wav_path, fps=fps, **envelope_kwargs)
    samples, sample_rate = _read_wav_as_float(wav_path)
    analysis, analysis_rate = _decimate(samples, sample_rate)

    window = max(_LPC_ORDER * 4, int(analysis_rate * (1.0 / fps) * 3.0))  # 約3フレーム分
    previous: dict[str, float] = {}
    for frame in frames:
        if frame.mouth_openness < 0.05:
            previous = {}
            continue
        center = int(frame.time_sec * analysis_rate)
        start = max(0, center - window // 2)
        segment = analysis[start:start + window]
        formants = estimate_formants(segment, analysis_rate)
        if formants is None:
            weights = previous  # 求まらなければ直前の母音を保つ(口形のちらつき防止)
        else:
            weights = classify_vowel(*formants)
            if previous:  # 母音の急変を抑える(時間方向の平滑化)
                weights = {
                    v: 0.5 * weights.get(v, 0.0) + 0.5 * previous.get(v, 0.0)
                    for v in VOWEL_FORMANTS
                }
        previous = weights
        frame.vowels = {v: w * frame.mouth_openness for v, w in weights.items()}
    return frames


def vowels_at(frames: list[LipSyncFrame], time_sec: float) -> dict[str, float]:
    """任意の時刻に最も近いフレームの母音の重みを返す(mouth_openness_atと同様)。"""
    if not frames:
        return {}
    fps_interval = frames[1].time_sec - frames[0].time_sec if len(frames) > 1 else 1.0
    index = int(round(time_sec / fps_interval)) if fps_interval > 0 else 0
    index = max(0, min(len(frames) - 1, index))
    return dict(frames[index].vowels)


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

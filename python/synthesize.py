"""aural Studio - テキスト → VO-SE音声合成 統合レイヤー

text_to_notes.py が生成する PhonemeNote 列(音素・尺・単一ピッチ)を、
vose_engine.py が要求する NoteEventData(フレーム単位のピッチカーブ)に
変換し、実際に execute_render() を呼び出して音声ファイルを生成する。

現時点では実録音の音素サンプル(ボイスバンク)が無いため、パイプライン
全体の疎通確認用に簡易的なプレースホルダー音源(倍音構成の合成波形)を
生成して使う。実際の声で合成するには、load_embedded_resource() に
本物のUTAU式ボイスバンクの録音データを登録する必要がある(TODO)。
"""

from __future__ import annotations

import math
import wave

import numpy as np
import pyopenjtalk

from text_to_notes import PhonemeNote, text_to_notes
from vose_engine import NoteEventData, VoseEngine

FRAME_PERIOD_MS = 5.0  # vose_core.cpp の kFramePeriod と揃える
PLACEHOLDER_SAMPLE_RATE = 44100
PLACEHOLDER_DURATION_SEC = 0.35  # どの音素の尺よりも長くしておく(最大0.13秒程度のため余裕を持たせる)
PLACEHOLDER_BASE_HZ = 220.0


def synthesize_narration(text: str, output_path: str) -> None:
    """「素の読み上げ」用の合成経路。VO-SE(vose_core)は一切経由せず、
    pyopenjtalkの内蔵HTS音声エンジン(tts())をそのまま使う。

    UTAU音源(録音済みボイスバンク)を使わない、Speaker.voice_source ==
    "openjtalk" の行はこちらで処理する。VO-SEエンジンのロードが一切
    発生しないため、台本が全てナレーションのみで構成されている場合は
    vose_core.so自体を読み込む必要がない。
    """
    samples_f64, sample_rate = pyopenjtalk.tts(text)
    # pyopenjtalk.tts()はfloat64で返すため、int16レンジにクリップしてから変換する
    # (通常は範囲内に収まるが、テキストによっては超えることもあるため念のため)。
    clipped = np.clip(samples_f64, -32768, 32767)
    samples_i16 = clipped.astype(np.int16)

    with wave.open(output_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples_i16.tobytes())


def generate_placeholder_sample(sample_rate: int = PLACEHOLDER_SAMPLE_RATE,
                                 duration_sec: float = PLACEHOLDER_DURATION_SEC,
                                 base_hz: float = PLACEHOLDER_BASE_HZ) -> list[int]:
    """録音音源が無い状態でパイプラインを疎通確認するための仮の励振波形。

    のこぎり波に近い倍音構成(基音+2倍音+3倍音)を持たせているが、
    あくまで「WORLDが解析・再合成できる音源」として機能するかを
    確認するためのプレースホルダーであり、声の質は一切考慮していない。
    実際の声で合成するには、UTAU形式のボイスバンク(録音済み音素サンプル)
    に置き換える必要がある。
    """
    n = int(sample_rate * duration_sec)
    samples: list[int] = []
    for t in range(n):
        phase = 2.0 * math.pi * base_hz * t / sample_rate
        value = (
            math.sin(phase)
            + 0.5 * math.sin(2 * phase)
            + 0.25 * math.sin(3 * phase)
        )
        # 3倍音までの合成で振幅が最大1.75程度になるため正規化してからスケール
        value /= 1.75
        samples.append(int(value * 28000))
    return samples


def register_placeholder_voicebank(engine: VoseEngine, phonemes: set[str]) -> None:
    """未登録の音素にプレースホルダー音源を割り当てて登録する。"""
    sample = generate_placeholder_sample()
    for phoneme in phonemes:
        engine.load_embedded_resource(phoneme, sample, PLACEHOLDER_SAMPLE_RATE)


def phoneme_notes_to_note_events(
    notes: list[PhonemeNote],
    frame_period_ms: float = FRAME_PERIOD_MS,
) -> list[NoteEventData]:
    """PhonemeNote列(音素・尺・単一ピッチ)を、VO-SEのNoteEventData列
    (フレーム単位のピッチカーブ)に変換する。

    各音素は現時点では一定ピッチ(text_to_notesが決めたH/Lの1値)のまま
    フレーム数分引き伸ばすだけで、ピッチの滑らかな遷移(ポルタメント)は
    まだ考慮していない(TODO: 音素境界での補間を後で追加する)。
    """
    events: list[NoteEventData] = []
    for note in notes:
        frame_count = max(1, round((note.duration_sec * 1000.0) / frame_period_ms))
        events.append(NoteEventData(
            wav_path=note.phoneme,
            pitch_curve_hz=[note.pitch_hz] * frame_count,
        ))
    return events


def synthesize_text(
    engine: VoseEngine,
    text: str,
    output_path: str,
    base_pitch_hz: float = 220.0,
    mode_flag: int = 0,
) -> list[PhonemeNote]:
    """日本語テキストを受け取り、VO-SEで音声ファイルとして書き出す。

    戻り値として、変換に使ったPhonemeNote列を返す(デバッグ表示用)。
    """
    notes = text_to_notes(text, base_pitch_hz=base_pitch_hz)
    if not notes:
        raise ValueError(f"テキストから音素を生成できませんでした: {text!r}")

    phonemes_used = {n.phoneme for n in notes}
    register_placeholder_voicebank(engine, phonemes_used)

    note_events = phoneme_notes_to_note_events(notes)
    engine.execute_render(note_events, output_path, mode_flag=mode_flag)
    return notes


if __name__ == "__main__":
    import sys

    lib_path = sys.argv[1] if len(sys.argv) > 1 else None
    text = sys.argv[2] if len(sys.argv) > 2 else "こんにちは"
    output_path = sys.argv[3] if len(sys.argv) > 3 else "/tmp/synthesize_output.wav"

    engine = VoseEngine(lib_path)
    print("VO-SE engine version:", engine.get_engine_version())

    notes = synthesize_text(engine, text, output_path)

    print(f"入力テキスト: {text}")
    print(f"音素数: {len(notes)}")
    for n in notes:
        print(f"  {n.mora_text:4s} {n.phoneme:4s} {n.duration_sec:.3f}s {n.pitch_hz:.1f}Hz")
    print(f"出力: {output_path}")

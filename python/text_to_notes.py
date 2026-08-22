"""aural Studio - テキスト → NoteEvent[] 変換レイヤー(プロトタイプ)

VO-SEエンジン(vose_core)が要求する NoteEvent[](音素・継続長・ピッチ・タイミング)
を、日本語テキストから自動生成する。

pyopenjtalk (r9y9/pyopenjtalk) を使うが、HTSEngineの内部duration/F0は
Python APIから取得できない(get_generated_speech()等、最終波形しか公開されて
いない)ため、代わりに以下を自前で行う:

  1. pyopenjtalk.run_frontend() で単語ごとの読み(カタカナ)・アクセント型
     (acc: 0=平板型, N=N拍目の後で下がる)・モーラ数を取得
  2. カタカナ読みを「モーラ」単位に分割し、モーラ→音素のテーブルで音素化
  3. 標準的な日本語アクセント規則(1拍目低→2拍目から高、アクセント核の後で
     下がる、平板型は下がらない)に従って、モーラごとにH/Lを割り当てる
  4. 音素の種類(母音/子音/撥音/促音など)ごとの概算継続長テーブルで
     duration(秒)を割り当てる
  5. H/Lを基準ピッチ(base_pitch_hz)からの半音オフセットに変換し、
     音素ごとのpitch_hzを確定する

現時点ではあくまで規則ベースの概算であり、実際のVO-SE合成結果を聴きながら
duration/pitch のパラメータを調整していく前提のプロトタイプ。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pyopenjtalk


# --- 音素の種類ごとの概算継続長(秒) ---
# TODO: 実際にVO-SEで合成して聴きながら調整する。あくまで初期値。
VOWELS = {"a", "i", "u", "e", "o"}
PHONEME_DURATION_SEC = {
    # 母音は長め
    "a": 0.13, "i": 0.11, "u": 0.10, "e": 0.12, "o": 0.13,
    # 撥音「ん」・促音「っ」相当
    "N": 0.10,   # 撥音(ん)
    "cl": 0.06,  # 促音(っ) - 直後の子音の閉鎖時間相当
    # 無声破裂音(短め)
    "k": 0.05, "t": 0.05, "p": 0.05,
    # 有声破裂音
    "g": 0.05, "d": 0.05, "b": 0.05,
    # 摩擦音
    "s": 0.08, "sh": 0.08, "z": 0.07, "h": 0.07, "f": 0.07,
    # 破擦音
    "ch": 0.08, "ts": 0.08, "j": 0.07,
    # 鼻音・流音・半母音
    "m": 0.06, "n": 0.06, "r": 0.05, "y": 0.05, "w": 0.05,
}
DEFAULT_PHONEME_DURATION_SEC = 0.07
PAUSE_DURATION_SEC = 0.25  # 文末・読点等の無音区間

# --- アクセント規則のピッチオフセット(半音) ---
# TODO: 実際に聴いて自然に聞こえる値へ調整する。あくまで初期値。
HIGH_SEMITONE_OFFSET = 3.0
LOW_SEMITONE_OFFSET = 0.0


@dataclass
class PhonemeNote:
    """VO-SEのNoteEventに渡す1音素分の情報。"""
    phoneme: str
    duration_sec: float
    pitch_hz: float
    mora_text: str = ""  # デバッグ表示用(この音素がどのモーラに属するか)


# --- カタカナ1モーラ → 音素列 の対応表 ---
# 拗音(ャュョ)を含む代表的な組み合わせのみ収録。
# TODO: 網羅性はまだ低い(方言的表記や外来語特有の拗音などは未対応)。
_MORA_TO_PHONEMES: dict[str, list[str]] = {}


def _build_mora_table() -> None:
    """五十音+拗音のモーラ→音素テーブルを機械的に組み立てる。"""
    base_consonants = {
        "": [], "カ": ["k"], "サ": ["s"], "タ": ["t"], "ナ": ["n"],
        "ハ": ["h"], "マ": ["m"], "ヤ": ["y"], "ラ": ["r"], "ワ": ["w"],
        "ガ": ["g"], "ザ": ["z"], "ダ": ["d"], "バ": ["b"], "パ": ["p"],
    }
    vowel_kana = {"ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o"}

    # 直音(カ行〜ワ行 + ガ行〜パ行)
    gyo_table = {
        "カ": "kasitehokantsu", "": "",
    }
    # 手作業で行ごとの母音マッピングを定義する方が確実なので、代表例を直接列挙する。
    rows = {
        "カ": ["k"], "キ": ["k"], "ク": ["k"], "ケ": ["k"], "コ": ["k"],
        "サ": ["s"], "シ": ["sh"], "ス": ["s"], "セ": ["s"], "ソ": ["s"],
        "タ": ["t"], "チ": ["ch"], "ツ": ["ts"], "テ": ["t"], "ト": ["t"],
        "ナ": ["n"], "ニ": ["n"], "ヌ": ["n"], "ネ": ["n"], "ノ": ["n"],
        "ハ": ["h"], "ヒ": ["h"], "フ": ["f"], "ヘ": ["h"], "ホ": ["h"],
        "マ": ["m"], "ミ": ["m"], "ム": ["m"], "メ": ["m"], "モ": ["m"],
        "ヤ": ["y"], "ユ": ["y"], "ヨ": ["y"],
        "ラ": ["r"], "リ": ["r"], "ル": ["r"], "レ": ["r"], "ロ": ["r"],
        "ワ": ["w"], "ヲ": [],
        "ガ": ["g"], "ギ": ["g"], "グ": ["g"], "ゲ": ["g"], "ゴ": ["g"],
        "ザ": ["z"], "ジ": ["j"], "ズ": ["z"], "ゼ": ["z"], "ゾ": ["z"],
        "ダ": ["d"], "ヂ": ["j"], "ヅ": ["z"], "デ": ["d"], "ド": ["d"],
        "バ": ["b"], "ビ": ["b"], "ブ": ["b"], "ベ": ["b"], "ボ": ["b"],
        "パ": ["p"], "ピ": ["p"], "プ": ["p"], "ペ": ["p"], "ポ": ["p"],
    }
    row_vowel = {
        "カ": "a", "キ": "i", "ク": "u", "ケ": "e", "コ": "o",
        "サ": "a", "シ": "i", "ス": "u", "セ": "e", "ソ": "o",
        "タ": "a", "チ": "i", "ツ": "u", "テ": "e", "ト": "o",
        "ナ": "a", "ニ": "i", "ヌ": "u", "ネ": "e", "ノ": "o",
        "ハ": "a", "ヒ": "i", "フ": "u", "ヘ": "e", "ホ": "o",
        "マ": "a", "ミ": "i", "ム": "u", "メ": "e", "モ": "o",
        "ヤ": "a", "ユ": "u", "ヨ": "o",
        "ラ": "a", "リ": "i", "ル": "u", "レ": "e", "ロ": "o",
        "ワ": "a", "ヲ": "o",
        "ガ": "a", "ギ": "i", "グ": "u", "ゲ": "e", "ゴ": "o",
        "ザ": "a", "ジ": "i", "ズ": "u", "ゼ": "e", "ゾ": "o",
        "ダ": "a", "ヂ": "i", "ヅ": "u", "デ": "e", "ド": "o",
        "バ": "a", "ビ": "i", "ブ": "u", "ベ": "e", "ボ": "o",
        "パ": "a", "ピ": "i", "プ": "u", "ペ": "e", "ポ": "o",
    }

    for kana, consonant in rows.items():
        vowel = row_vowel[kana]
        _MORA_TO_PHONEMES[kana] = consonant + [vowel]

    for kana, vowel in vowel_kana.items():
        _MORA_TO_PHONEMES[kana] = [vowel]

    # 拗音(キャ/シャ/チャ...): 子音+ャ/ュ/ョ の組を子音+母音1つのモーラとして扱う
    yoon_base = {
        "キ": "k", "シ": "sh", "チ": "ch", "ニ": "n", "ヒ": "h", "ミ": "m",
        "リ": "r", "ギ": "g", "ジ": "j", "ビ": "b", "ピ": "p",
    }
    yoon_vowel = {"ャ": "a", "ュ": "u", "ョ": "o"}
    for base_kana, consonant in yoon_base.items():
        for small_kana, vowel in yoon_vowel.items():
            _MORA_TO_PHONEMES[base_kana + small_kana] = [consonant, vowel]

    # 特殊モーラ
    _MORA_TO_PHONEMES["ン"] = ["N"]
    _MORA_TO_PHONEMES["ッ"] = ["cl"]


_build_mora_table()

_SMALL_YOON_KANA = {"ャ", "ュ", "ョ"}


def split_kana_to_morae(kana: str) -> list[str]:
    """カタカナ読みを1モーラずつのリストに分割する。

    「ー」(長音符)は直前のモーラの母音を伸ばす記号として扱い、
    独立したモーラにはせず直前モーラに連結する(音素化の際に母音を
    重複させる)。
    """
    morae: list[str] = []
    i = 0
    while i < len(kana):
        ch = kana[i]
        if ch == "ー":
            # 長音符: 直前モーラの母音を1つ追加した形にする(擬似的に伸ばす)
            if morae:
                morae.append("ー")
            i += 1
            continue

        # 拗音(次の文字が小書きのャ/ュ/ョ)なら2文字で1モーラ
        if i + 1 < len(kana) and kana[i + 1] in _SMALL_YOON_KANA:
            morae.append(kana[i:i + 2])
            i += 2
            continue

        morae.append(ch)
        i += 1
    return morae


def mora_to_phonemes(mora: str) -> list[str]:
    if mora == "ー":
        return []  # 呼び出し側で直前モーラの母音を伸長する形で処理する
    return _MORA_TO_PHONEMES.get(mora, [])


def _group_into_accent_phrases(words: list[dict]) -> list[dict]:
    """run_frontend()の単語リストを「アクセント句」単位にまとめる。

    OpenJTalkは助詞・助動詞などを、直前の内容語と同じアクセント句として
    chain_flag=1で報告する(例: 「橋」+「が」で1つのアクセント句)。
    アクセント規則(1拍目低→2拍目から高、核の後で下がる)は単語単位ではなく
    このアクセント句単位で適用しないと、句をまたいだ際にH/Lを誤る
    (例: 「橋が」と「端が」で「が」のH/Lが区別できなくなる)。

    句の先頭語(chain_flag != 1)が報告する acc は、既に結合後の句全体での
    アクセント核位置を指している(例: 「行き」単体はmora_size=2なのに
    acc=3になるのは、後続の「ます」まで含めた句全体での核位置だから)。
    そのため、句をまとめた後は先頭語のaccをそのまま句全体のaccとして使う。
    """
    phrases: list[dict] = []
    for word in words:
        pron = word.get("pron", "") or word.get("read", "")
        if not pron:
            continue
        if word.get("chain_flag") == 1 and phrases:
            # 直前の句に結合する
            phrases[-1]["pron"] += pron
        else:
            # 新しいアクセント句を開始する(先頭語のaccを句全体のaccとして採用)
            phrases.append({"pron": pron, "acc": word.get("acc", 0)})
    return phrases


def text_to_notes(text: str, base_pitch_hz: float = 220.0) -> list[PhonemeNote]:
    """日本語テキストを、VO-SEのNoteEventに渡せる音素列(PhonemeNoteのリスト)に変換する。

    Args:
        text: 変換したい日本語テキスト(1文程度を想定)。
        base_pitch_hz: 「低い」ピッチの基準周波数(Hz)。「高い」はここから
            HIGH_SEMITONE_OFFSET半音分上げた値になる。
    """
    words = pyopenjtalk.run_frontend(text)
    phrases = _group_into_accent_phrases(words)
    notes: list[PhonemeNote] = []

    for phrase in phrases:
        pron = phrase["pron"]
        acc = phrase["acc"]

        morae = split_kana_to_morae(pron)
        mora_count = len(morae)
        if mora_count == 0:
            continue

        for mora_index, mora in enumerate(morae, start=1):  # 1始まり
            if mora == "ー":
                # 長音符: 直前モーラの母音を伸ばす(同じ音素をもう1つ追加)
                if notes and notes[-1].phoneme in VOWELS:
                    prev = notes[-1]
                    notes.append(PhonemeNote(
                        phoneme=prev.phoneme,
                        duration_sec=PHONEME_DURATION_SEC.get(prev.phoneme, DEFAULT_PHONEME_DURATION_SEC),
                        pitch_hz=prev.pitch_hz,
                        mora_text="ー",
                    ))
                continue

            phonemes = mora_to_phonemes(mora)
            if not phonemes:
                continue

            # --- アクセント規則: モーラのH/Lを決定 ---
            # acc == 0 (平板型): 1拍目だけ低く、2拍目以降はずっと高い(下がらない)
            # acc == N (N拍目の後で下がる): 1拍目は低い(N==1の場合を除く)、
            #   2拍目〜N拍目までが高く、N+1拍目以降は低い
            if acc == 0:
                is_high = mora_index >= 2
            else:
                if mora_index == 1:
                    is_high = (acc == 1)
                else:
                    is_high = mora_index <= acc

            pitch_hz = base_pitch_hz * (2.0 ** ((HIGH_SEMITONE_OFFSET if is_high else LOW_SEMITONE_OFFSET) / 12.0))

            for ph in phonemes:
                duration = PHONEME_DURATION_SEC.get(ph, DEFAULT_PHONEME_DURATION_SEC)
                notes.append(PhonemeNote(
                    phoneme=ph,
                    duration_sec=duration,
                    pitch_hz=pitch_hz,
                    mora_text=mora,
                ))

    return notes


def total_duration_sec(notes: list[PhonemeNote]) -> float:
    return sum(n.duration_sec for n in notes)


if __name__ == "__main__":
    import sys

    text = sys.argv[1] if len(sys.argv) > 1 else "橋を渡って端まで歩く"
    notes = text_to_notes(text)

    print(f"入力テキスト: {text}")
    print(f"音素数: {len(notes)}  推定合計時間: {total_duration_sec(notes):.2f}秒")
    print()
    print(f"{'モーラ':<6}{'音素':<6}{'尺(秒)':<10}{'ピッチ(Hz)':<12}")
    for n in notes:
        print(f"{n.mora_text:<6}{n.phoneme:<6}{n.duration_sec:<10.3f}{n.pitch_hz:<12.1f}")

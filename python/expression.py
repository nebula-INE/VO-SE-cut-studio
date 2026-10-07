"""aural Studio - 表情(ブレンドシェイプ)名の正規化と状態管理

VMCプロトコルで送られてくるブレンドシェイプ名は、送信アプリやVRMのバージョンに
よって表記が揺れる(例: VRM0.x系は "Joy"/"A"、VRM1.0系は "happy"/"aa")。
ここで内部の標準名に正規化し、キャラクター描画側(character_renderer.py)が
表記揺れを気にせず使えるようにする。

内部の標準名:
    表情   : Joy, Angry, Sorrow, Fun
    まばたき: Blink, Blink_L, Blink_R
    母音   : A, I, U, E, O
未知の名前は無視せず、そのまま(元の名前で)保持する(将来の拡張用)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

VOWELS = ("A", "I", "U", "E", "O")

# 小文字化した入力名 -> 標準名
_ALIASES: dict[str, str] = {
    # 表情(VRM0.x / VRoid)
    "joy": "Joy", "angry": "Angry", "sorrow": "Sorrow", "fun": "Fun",
    # 表情(VRM1.0 / ARKit系アプリが使う名前)
    "happy": "Joy", "sad": "Sorrow", "relaxed": "Fun", "surprised": "Surprised",
    # まばたき
    "blink": "Blink", "blink_l": "Blink_L", "blink_r": "Blink_R",
    "blinkleft": "Blink_L", "blinkright": "Blink_R",
    # 母音(VRM0.x: A/I/U/E/O、VRM1.0: aa/ih/ou/ee/oh)
    "a": "A", "i": "I", "u": "U", "e": "E", "o": "O",
    "aa": "A", "ih": "I", "ou": "U", "ee": "E", "oh": "O",
}


def normalize_name(name: str) -> str:
    """ブレンドシェイプ名を標準名にする。未知の名前はそのまま返す。"""
    return _ALIASES.get(name.strip().lower(), name)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_blend_shapes(raw: dict[str, float]) -> dict[str, float]:
    """生のブレンドシェイプ辞書を標準名・0〜1にクランプした辞書へ変換する。

    同じ標準名に複数の別名が来た場合は、大きい方の値を採用する。
    """
    result: dict[str, float] = {}
    for name, value in raw.items():
        key = normalize_name(name)
        v = _clamp01(value)
        result[key] = max(result.get(key, 0.0), v)
    return result


@dataclass
class Expression:
    """描画に使う表情の状態(すべて0〜1)。"""

    joy: float = 0.0
    angry: float = 0.0
    sorrow: float = 0.0
    fun: float = 0.0
    blink: float = 0.0
    vowels: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_blend_shapes(cls, raw: dict[str, float] | None) -> "Expression":
        shapes = normalize_blend_shapes(raw or {})
        if "Blink" in shapes:
            blink = shapes["Blink"]
        else:
            sides = [shapes[k] for k in ("Blink_L", "Blink_R") if k in shapes]
            blink = sum(sides) / len(sides) if sides else 0.0
        return cls(
            joy=shapes.get("Joy", 0.0),
            angry=shapes.get("Angry", 0.0),
            sorrow=shapes.get("Sorrow", 0.0),
            fun=shapes.get("Fun", 0.0),
            blink=blink,
            vowels={v: shapes[v] for v in VOWELS if shapes.get(v, 0.0) > 0.0},
        )

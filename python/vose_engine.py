"""aural Studio - VO-SEエンジン(vose_core) ctypes呼び出しレイヤー

vose_core.dll/.so/.dylib をctypes経由でロードし、Pythonから安全に呼び出す
ための薄いラッパー。構造体レイアウトは include/vose_core.h と1バイトも
違わないよう手動で対応させている(フィールド順・型・#pragma pack指定を
ヘッダ側が変わったら必ずここも追従すること)。

現時点では、テキスト→音素/ピッチ変換(text_to_notes.py)から得られる
PhonemeNote列を、VO-SEのNoteEvent配列へ変換して execute_render() を
呼び出すところまでを実装する。

使い方:
    engine = VoseEngine("/path/to/libvose_core.so")
    engine.set_oto_data([...])                # 音源のタイミング情報(任意)
    engine.load_embedded_resource("a", pcm_samples, 44100)
    engine.execute_render(note_events, "output.wav", mode_flag=0)
"""

from __future__ import annotations

import ctypes
import platform
from dataclasses import dataclass, field
from pathlib import Path


# --- vose_core.h の構造体定義(1:1対応、フィールド順厳守) ---

class OtoEntry(ctypes.Structure):
    # #pragma pack指定なし(デフォルトアライメント)
    _fields_ = [
        ("filename", ctypes.c_char_p),
        ("cutoff", ctypes.c_double),
        ("alias", ctypes.c_char * 64),
        ("wav_path", ctypes.c_char * 512),
        ("offset", ctypes.c_double),
        ("consonant", ctypes.c_double),
        ("blank", ctypes.c_double),
        ("preutterance", ctypes.c_double),
        ("overlap", ctypes.c_double),
    ]


class VoseFrame(ctypes.Structure):
    # vose_core.h側は #pragma pack(push, 8) で8バイト境界に整列されている。
    _pack_ = 8
    _fields_ = [
        ("time", ctypes.c_double),
        ("phoneme", ctypes.c_char * 8),
        ("weight", ctypes.c_double),
    ]


class NoteEvent(ctypes.Structure):
    _pack_ = 8
    _fields_ = [
        ("wav_path", ctypes.c_char_p),
        ("pitch_curve", ctypes.POINTER(ctypes.c_double)),
        ("pitch_length", ctypes.c_int),
        ("gender_curve", ctypes.POINTER(ctypes.c_double)),
        ("tension_curve", ctypes.POINTER(ctypes.c_double)),
        ("breath_curve", ctypes.POINTER(ctypes.c_double)),
        ("vibrato_depth_curve", ctypes.POINTER(ctypes.c_double)),
        ("vibrato_rate_curve", ctypes.POINTER(ctypes.c_double)),
        ("vibrato_curve_length", ctypes.c_int),
        ("portamento_offsets", ctypes.POINTER(ctypes.c_double)),
        ("portamento_length", ctypes.c_int),
    ]


@dataclass
class NoteEventData:
    """NoteEvent構造体を組み立てるためのPython側の中間データ。

    pitch_curve等はHzの配列(フレーム単位)を想定。ctypes配列への変換は
    VoseEngine側で行う(呼び出し側はPythonのlist[float]で渡せばよい)。
    """
    wav_path: str
    pitch_curve_hz: list[float]
    gender_curve: list[float] | None = None
    tension_curve: list[float] | None = None
    breath_curve: list[float] | None = None
    vibrato_depth_curve: list[float] = field(default_factory=list)
    vibrato_rate_curve: list[float] = field(default_factory=list)
    portamento_offsets: list[float] = field(default_factory=list)


def _default_lib_name() -> str:
    system = platform.system()
    if system == "Windows":
        return "vose_core.dll"
    if system == "Darwin":
        return "libvose_core.dylib"
    return "libvose_core.so"


class VoseEngine:
    """vose_coreライブラリへのctypesラッパー。"""

    def __init__(self, lib_path: str | None = None) -> None:
        path = lib_path or _default_lib_name()
        self._lib = ctypes.CDLL(str(path))
        self._bind_signatures()

        # keepalive: ctypesに渡したバッファがGCされないよう、呼び出し中は
        # ここで参照を保持しておく(呼び出し完了後にクリアする)。
        self._keepalive: list[object] = []

    def _bind_signatures(self) -> None:
        lib = self._lib

        lib.set_oto_data.argtypes = [ctypes.POINTER(OtoEntry), ctypes.c_int]
        lib.set_oto_data.restype = None

        lib.load_embedded_resource.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ]
        lib.load_embedded_resource.restype = None

        lib.execute_render.argtypes = [
            ctypes.POINTER(NoteEvent), ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
        ]
        lib.execute_render.restype = None

        lib.set_vocal_timeline.argtypes = [ctypes.POINTER(VoseFrame), ctypes.c_int]
        lib.set_vocal_timeline.restype = None

        lib.get_engine_version.argtypes = []
        lib.get_engine_version.restype = ctypes.c_float

        lib.clear_engine_cache.argtypes = []
        lib.clear_engine_cache.restype = None

        lib.set_bigvgan_model.argtypes = [ctypes.c_char_p]
        lib.set_bigvgan_model.restype = None

    # --- 公開API ---

    def get_engine_version(self) -> float:
        return float(self._lib.get_engine_version())

    def clear_engine_cache(self) -> None:
        self._lib.clear_engine_cache()

    def set_oto_data(self, entries: list[dict]) -> None:
        """entries: [{"filename", "cutoff", "alias", "wav_path", "offset",
        "consonant", "blank", "preutterance", "overlap"}, ...]
        """
        count = len(entries)
        arr = (OtoEntry * count)()
        # filenameのbytesはOtoEntry生存期間中GCされないよう保持しておく必要がある
        keep: list[bytes] = []
        for i, e in enumerate(entries):
            filename_bytes = str(e.get("filename", "")).encode("utf-8")
            keep.append(filename_bytes)
            arr[i].filename = filename_bytes
            arr[i].cutoff = float(e.get("cutoff", 0.0))
            arr[i].alias = str(e.get("alias", "")).encode("utf-8")
            arr[i].wav_path = str(e.get("wav_path", "")).encode("utf-8")
            arr[i].offset = float(e.get("offset", 0.0))
            arr[i].consonant = float(e.get("consonant", 0.0))
            arr[i].blank = float(e.get("blank", 0.0))
            arr[i].preutterance = float(e.get("preutterance", 0.0))
            arr[i].overlap = float(e.get("overlap", 0.0))

        self._lib.set_oto_data(arr, count)
        # C++側はset_oto_data内でg_oto_dbへコピーしているため、呼び出しが
        # 完了すればkeep/arrは不要(即時解放して問題ない)。

    def load_embedded_resource(self, phoneme: str, samples: list[int], sample_rate: int) -> None:
        """samples: 16bit PCM整数のリスト(int16範囲)。"""
        count = len(samples)
        arr = (ctypes.c_int16 * count)(*samples)
        phoneme_bytes = phoneme.encode("utf-8")
        self._lib.load_embedded_resource(phoneme_bytes, arr, count)

    def execute_render(self, notes: list[NoteEventData], output_path: str, mode_flag: int = 0) -> None:
        """notes: NoteEventDataのリスト。output_path: 出力wavファイルパス。"""
        count = len(notes)
        c_notes = (NoteEvent * count)()

        # ctypes配列はPythonオブジェクトのGCと連動しないため、execute_render()
        #呼び出しが完了するまで全ての中間配列をkeepaliveリストで保持する。
        keep: list[object] = []

        for i, note in enumerate(notes):
            wav_path_bytes = note.wav_path.encode("utf-8")
            keep.append(wav_path_bytes)
            c_notes[i].wav_path = wav_path_bytes

            pitch_arr = (ctypes.c_double * len(note.pitch_curve_hz))(*note.pitch_curve_hz)
            keep.append(pitch_arr)
            c_notes[i].pitch_curve = pitch_arr
            c_notes[i].pitch_length = len(note.pitch_curve_hz)

            def _optional_curve(values: list[float] | None):
                if not values:
                    return None
                arr = (ctypes.c_double * len(values))(*values)
                keep.append(arr)
                return arr

            c_notes[i].gender_curve = _optional_curve(note.gender_curve)
            c_notes[i].tension_curve = _optional_curve(note.tension_curve)
            c_notes[i].breath_curve = _optional_curve(note.breath_curve)

            vib_depth = _optional_curve(note.vibrato_depth_curve)
            vib_rate = _optional_curve(note.vibrato_rate_curve)
            c_notes[i].vibrato_depth_curve = vib_depth
            c_notes[i].vibrato_rate_curve = vib_rate
            c_notes[i].vibrato_curve_length = len(note.vibrato_depth_curve) if note.vibrato_depth_curve else 0

            porta = _optional_curve(note.portamento_offsets)
            c_notes[i].portamento_offsets = porta
            c_notes[i].portamento_length = len(note.portamento_offsets) if note.portamento_offsets else 0

        output_bytes = str(output_path).encode("utf-8")
        keep.append(output_bytes)

        self._lib.execute_render(c_notes, count, output_bytes, mode_flag)
        # 呼び出し完了(=execute_renderが同期的にレンダリング処理を終えて
        # 戻ってきた)後は、keepの中身は不要になるので明示的な後始末は無し
        # (関数を抜ければ自然にGC対象になる)。


if __name__ == "__main__":
    import sys

    lib_path = sys.argv[1] if len(sys.argv) > 1 else None
    engine = VoseEngine(lib_path)
    print("VO-SE engine version:", engine.get_engine_version())

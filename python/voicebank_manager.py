"""aural Studio - ボイスバンク管理(UTAU音源 / 公式音源)

VO-SEエンジン(vose_core)は、音源の与え方として2つの経路を持っている:

    1. UTAU形式のボイスバンク: oto.ini(タイミング情報)+ 実ファイルの
       wavを組み合わせたもの。set_oto_data()でタイミング情報を渡す。

    2. 「公式音源」的な、アプリに同梱・埋め込まれた音源: load_embedded_
       resource()で音素ごとのPCMデータを直接メモリに登録する。

このモジュールは、この2種類のボイスバンクを同じ`VoiceBank`という枠組みで
扱えるようにし、種別ごとのローダーに処理を委譲する。

現時点では UTAU形式のローダー(oto.iniパーサー)のみ実装済み。公式音源の
具体的なファイル形式はまだ決まっていないため、後から`register_official_
loader()`で差し込めるよう、拡張ポイントだけを用意してある(登録するまでは、
公式音源タイプのボイスバンクはロード時にNotImplementedErrorになる)。

[重要な実装メモ] 当初、UTAU音源のロードをset_oto_data()(タイミング情報)
だけで実装したところ、無音のwavが生成されるバグを実際に踏んだ。VO-SEでは
OtoEntryは純粋にタイミングのメタデータであり、実際の音声データ(PCM)は
別途load_embedded_resource()でエイリアス名をキーとして明示的に登録する
必要がある。_load_utau_voicebank()はこの両方を行う。
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vose_engine import VoseEngine

# VoiceBank.type の値。"utau"は今回実装、"official"は拡張ポイントのみ用意。
VOICEBANK_TYPE_UTAU = "utau"
VOICEBANK_TYPE_OFFICIAL = "official"


@dataclass
class VoiceBank:
    """1つのボイスバンクのメタデータ。"""
    id: str
    name: str
    type: str  # VOICEBANK_TYPE_UTAU または VOICEBANK_TYPE_OFFICIAL
    path: str  # UTAU: oto.iniを含むフォルダ。公式音源: 実装時に決める形式に依存。


@dataclass
class OtoEntryData:
    """oto.ini 1行分のデータ(vose_engine.OtoEntryへ変換する前のPython側の中間表現)。"""
    filename: str      # 実ファイル名(例: "_あ.wav")
    alias: str         # エイリアス(例: "あ")。NoteEventData.wav_pathで参照する名前。
    offset_ms: float
    consonant_ms: float
    blank_ms: float
    preutterance_ms: float
    overlap_ms: float


def parse_oto_ini(oto_ini_path: str) -> list[OtoEntryData]:
    """UTAU形式のoto.iniファイルをパースする。

    各行の形式: filename=alias,offset,consonant,blank,preutterance,overlap
        例: _あ.wav=あ,20,30,20,40,20

    エンコーディングについて: 伝統的なUTAU音源のoto.iniはShift-JIS(CP932)で
    書かれていることが多いが、近年のツールはUTF-8で書き出すことも増えている。
    UTF-8としてまず読み込みを試み、失敗したらCP932にフォールバックする。

    [重要な制約: preutteranceとノート尺の関係] VO-SEでは、ノート(1モーラ分の
    NoteEvent)の再生時間がoto.iniのpreutterance(先行発声)以下だと、実際に
    鳴らす部分が残らず無音になることを実測で確認している
    (例: ノート尺200ms・preutterance200msの組み合わせでは無音、
    ノート尺400ms・preutterance200msでは正常に鳴った)。
    aural Studioのtext_to_notes.py(ナレーション用)が生成するモーラの尺は
    概算で100〜150ms程度と短いため、ボイスバンク側のpreutteranceは
    それより十分短い値(目安として数十ms程度)にしておく必要がある。
    歌唱用に長い音符を想定して作られた一般的なUTAU音源(preutteranceが
    100〜300ms程度のものも珍しくない)を、そのままナレーション用途に
    転用すると無音になりやすいので注意すること。
    """
    path = Path(oto_ini_path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp932")

    entries: list[OtoEntryData] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or "=" not in line:
            continue

        filename, _, rest = line.partition("=")
        params = rest.split(",")
        if len(params) != 6:
            # 不正な行はスキップする(壊れたoto.ini全体を諦めさせるより、
            # 読める行だけでも使えた方が実用上ましなため)。
            print(f"[voicebank] oto.ini {line_no}行目: パラメータ数が不正なためスキップ: {raw_line!r}")
            continue

        alias, offset, consonant, blank, preutterance, overlap = params
        try:
            entries.append(OtoEntryData(
                filename=filename.strip(),
                alias=alias.strip() or Path(filename.strip()).stem,  # alias省略時はファイル名を使う
                offset_ms=float(offset),
                consonant_ms=float(consonant),
                blank_ms=float(blank),
                preutterance_ms=float(preutterance),
                overlap_ms=float(overlap),
            ))
        except ValueError:
            print(f"[voicebank] oto.ini {line_no}行目: 数値変換に失敗したためスキップ: {raw_line!r}")
            continue

    return entries


def _read_wav_as_int16_samples(wav_path: Path) -> tuple[list[int], int]:
    """16bit PCMのWAVファイルを読み込み、(サンプル列, サンプルレート)を返す。"""
    with wave.open(str(wav_path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"16bit PCM以外のWAVには未対応です: {wav_path} (sampwidth={w.getsampwidth()})")
        sample_rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
        n_channels = w.getnchannels()

    samples = list(struct.unpack(f"<{n_frames * n_channels}h", raw))
    if n_channels > 1:
        # モノラルに落とす(チャンネル平均)。VO-SEはモノラル音源を前提としている。
        samples = [
            sum(samples[i:i + n_channels]) // n_channels
            for i in range(0, len(samples), n_channels)
        ]
    return samples, sample_rate


def _load_utau_voicebank(engine: VoseEngine, voicebank: VoiceBank) -> None:
    """UTAU形式のボイスバンクをVoseEngineに登録する。

    voicebank.path 直下に oto.ini と、そこで参照されているwavファイル群が
    置かれている前提(標準的なUTAU音源フォルダのレイアウト)。

    [重要] set_oto_data()だけではタイミング情報(オフセット等の
    メタデータ)しかVoseEngineに渡らず、肝心の音声データ(PCM)は登録
    されない(実際にset_oto_data単体で試したところ、無音のwavが生成
    されることを確認した)。そのため、oto.iniの各エントリについて、
    実ファイルを読み込みload_embedded_resource()で明示的にPCMも登録する
    必要がある。
    """
    bank_dir = Path(voicebank.path)
    oto_ini_path = bank_dir / "oto.ini"
    if not oto_ini_path.exists():
        raise FileNotFoundError(f"oto.iniが見つかりません: {oto_ini_path}")

    oto_entries = parse_oto_ini(str(oto_ini_path))
    if not oto_entries:
        raise ValueError(f"oto.iniから有効なエントリを1件も読み取れませんでした: {oto_ini_path}")

    dict_entries = []
    for e in oto_entries:
        wav_path = bank_dir / e.filename
        dict_entries.append({
            "filename": e.filename,
            "alias": e.alias,
            "wav_path": str(wav_path),
            "offset": e.offset_ms,
            "consonant": e.consonant_ms,
            # [注意] vose_engine.OtoEntryには"blank"と"cutoff"が別フィールドとして
            # 存在するが、標準的なoto.ini形式には"blank"(4番目の数値パラメータ、
            # UTAU用語では「オーバーラップ前の空白」)しか無く、"cutoff"に相当する
            # 独立した値は無い。UTAUツールによっては同じ4番目のパラメータを
            # 「cutoff」と呼ぶ流儀もあるため、ひとまず両フィールドに同じ値を
            # 入れている。エンジン側のcutoffの実際の意味づけが判明次第、
            # ここを調整すること(TODO)。
            "blank": e.blank_ms,
            "cutoff": e.blank_ms,
            "preutterance": e.preutterance_ms,
            "overlap": e.overlap_ms,
        })

        # タイミング情報(上記)とは別に、実際のPCMデータをエイリアス名で登録する。
        if not wav_path.exists():
            print(f"[voicebank] oto.iniが参照するwavファイルが見つかりません(スキップ): {wav_path}")
            continue
        try:
            samples, sample_rate = _read_wav_as_int16_samples(wav_path)
            engine.load_embedded_resource(e.alias, samples, sample_rate)
        except (ValueError, OSError, wave.Error) as ex:
            print(f"[voicebank] {wav_path} の読み込みに失敗(スキップ): {ex}")
            continue

    engine.set_oto_data(dict_entries)


# --- 公式音源用の拡張ポイント ---
# register_official_loader()で実装を差し込むまでは、公式音源タイプの
# ボイスバンクをロードしようとするとNotImplementedErrorになる。
_official_loader: Callable[[VoseEngine, VoiceBank], None] | None = None


def register_official_loader(loader: Callable[[VoseEngine, VoiceBank], None]) -> None:
    """公式音源のローダーを登録する。具体的なファイル形式が決まり次第、
    この関数にローダー実装を渡して呼び出すことで、VoicebankManagerが
    type="official"のボイスバンクを扱えるようになる。

    loader(engine, voicebank) は、voicebank.pathから音源データを読み込み、
    engine.load_embedded_resource()(または必要に応じてengine.set_oto_data())
    を呼んでVoseEngineへ登録する責務を持つ。
    """
    global _official_loader
    _official_loader = loader


def _load_official_voicebank(engine: VoseEngine, voicebank: VoiceBank) -> None:
    if _official_loader is None:
        raise NotImplementedError(
            "公式音源のローダーはまだ登録されていません。"
            "register_official_loader()で実装を登録してください。"
        )
    _official_loader(engine, voicebank)


_LOADERS: dict[str, Callable[[VoseEngine, VoiceBank], None]] = {
    VOICEBANK_TYPE_UTAU: _load_utau_voicebank,
    VOICEBANK_TYPE_OFFICIAL: _load_official_voicebank,
}


class VoicebankManager:
    """インストール済みボイスバンクの一覧管理と、VoseEngineへのロードを行う。

    ボイスバンクの発見(discover_voicebanks)は、指定フォルダ直下の
    サブフォルダそれぞれについて、"voicebank.json"(下記の形式)があれば
    それを読み、無ければoto.iniの有無からUTAU音源と推測する:

        voicebank.json の例:
            {"name": "サンプル音源", "type": "utau"}
    """

    def __init__(self) -> None:
        self._voicebanks: dict[str, VoiceBank] = {}
        self._loaded_into: dict[int, str] = {}  # id(engine) -> 直近ロードしたvoicebank.id

    def register(self, voicebank: VoiceBank) -> None:
        self._voicebanks[voicebank.id] = voicebank

    def get(self, voicebank_id: str) -> VoiceBank | None:
        return self._voicebanks.get(voicebank_id)

    def list_voicebanks(self) -> list[VoiceBank]:
        return list(self._voicebanks.values())

    def discover_voicebanks(self, root_dir: str) -> list[VoiceBank]:
        """root_dir直下のサブフォルダをスキャンし、見つかったボイスバンクを
        登録する。登録した一覧を返す。
        """
        found: list[VoiceBank] = []
        root = Path(root_dir)
        if not root.is_dir():
            return found

        for sub_dir in sorted(root.iterdir()):
            if not sub_dir.is_dir():
                continue

            manifest_path = sub_dir / "voicebank.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[voicebank] {manifest_path} の読み込みに失敗: {e}")
                    continue
                voicebank = VoiceBank(
                    id=sub_dir.name,
                    name=manifest.get("name", sub_dir.name),
                    type=manifest.get("type", VOICEBANK_TYPE_UTAU),
                    path=str(sub_dir),
                )
            elif (sub_dir / "oto.ini").exists():
                # voicebank.jsonが無くてもoto.iniがあればUTAU音源とみなす
                # (多くのUTAU音源配布はvoicebank.json相当のメタファイルを
                # 持たないため、このフォールバックで実用上困らないようにする)。
                voicebank = VoiceBank(id=sub_dir.name, name=sub_dir.name, type=VOICEBANK_TYPE_UTAU, path=str(sub_dir))
            else:
                continue

            self.register(voicebank)
            found.append(voicebank)

        return found

    def import_voicebank(
        self,
        source_path: str,
        root_dir: str,
        display_name: str | None = None,
    ) -> VoiceBank:
        """外部のボイスバンク(フォルダ、またはZIPファイル)を、
        root_dir配下にコピー(ZIPの場合は展開)して取り込む。

        GUIの「ボイスバンクを追加」機能から呼ばれる想定。取り込み後は
        自動でregister()され、他のボイスバンクと同様に扱えるようになる。

        Args:
            source_path: 取り込み元。フォルダ(oto.ini+wav群を含む)、
                または.zipファイル(展開するとoto.ini+wav群が出てくるもの)。
            root_dir: ボイスバンクの格納先ルート(通常はDEFAULT_VOICEBANK_ROOT)。
            display_name: 表示名。省略時はフォルダ名/zipファイル名を使う。

        Returns:
            取り込んだVoiceBank。

        Raises:
            FileNotFoundError: source_pathが存在しない。
            ValueError: 取り込んだ内容にoto.iniが見つからない
                (UTAU音源として不完全、または対応外の構成)。
        """
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"インポート元が見つかりません: {source_path}")

        root = Path(root_dir)
        root.mkdir(parents=True, exist_ok=True)

        base_name = display_name or source.stem
        voicebank_id = self._unique_voicebank_id(root, base_name)
        dest_dir = root / voicebank_id

        if source.is_dir():
            shutil.copytree(source, dest_dir)
        elif source.suffix.lower() == ".zip":
            dest_dir.mkdir(parents=True)
            with zipfile.ZipFile(source, "r") as zf:
                # ZIPスラッシュ("../"等)によるディレクトリトラバーサルを
                # 防ぐため、各エントリの展開先がdest_dir配下に収まることを
                # 確認してから展開する。
                for member in zf.namelist():
                    member_path = (dest_dir / member).resolve()
                    if not str(member_path).startswith(str(dest_dir.resolve())):
                        raise ValueError(f"不正なパスを含むZIPファイルです: {member!r}")
                zf.extractall(dest_dir)
        else:
            raise ValueError(f"フォルダまたは.zipファイルを指定してください: {source_path}")

        # oto.iniが直下ではなく1階層下の単一フォルダに入っているZIP構成
        # (多くの配布ZIPがこの形になっている)にも対応する。
        oto_ini_dir = self._find_oto_ini_dir(dest_dir)
        if oto_ini_dir is None:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise ValueError(
                f"oto.iniが見つからないため、UTAU音源として取り込めませんでした: {source_path}"
            )
        if oto_ini_dir != dest_dir:
            # 1階層下に実体がある場合は、中身をdest_dir直下へ引き上げる。
            for item in oto_ini_dir.iterdir():
                shutil.move(str(item), str(dest_dir / item.name))
            shutil.rmtree(oto_ini_dir, ignore_errors=True)

        voicebank = VoiceBank(
            id=voicebank_id,
            name=display_name or base_name,
            type=VOICEBANK_TYPE_UTAU,
            path=str(dest_dir),
        )
        self.register(voicebank)
        return voicebank

    @staticmethod
    def _find_oto_ini_dir(root: Path) -> Path | None:
        """rootまたはその1階層下のサブフォルダで、oto.iniを含むディレクトリを探す。"""
        if (root / "oto.ini").exists():
            return root
        if root.is_dir():
            sub_dirs = [d for d in root.iterdir() if d.is_dir()]
            if len(sub_dirs) == 1 and (sub_dirs[0] / "oto.ini").exists():
                return sub_dirs[0]
        return None

    @staticmethod
    def _unique_voicebank_id(root: Path, base_name: str) -> str:
        """他のボイスバンクIDと衝突しない、ファイル名として安全なIDを作る。"""
        safe_base = re.sub(r"[^\w\-]+", "_", base_name).strip("_") or "voicebank"
        candidate = safe_base
        suffix = 1
        while (root / candidate).exists():
            suffix += 1
            candidate = f"{safe_base}_{suffix}"
        return candidate

    def load_into_engine(self, engine: VoseEngine, voicebank_id: str) -> None:
        """指定したボイスバンクをVoseEngineへロードする(set_oto_data等)。

        同じengineインスタンスに対して、直前にロード済みのボイスバンクと
        同じidであれば再ロードをスキップする(1行ごとに毎回oto.iniを
        読み直すような無駄を避けるため)。
        """
        if self._loaded_into.get(id(engine)) == voicebank_id:
            return

        voicebank = self.get(voicebank_id)
        if voicebank is None:
            raise KeyError(f"未登録のボイスバンクIDです: {voicebank_id}")

        loader = _LOADERS.get(voicebank.type)
        if loader is None:
            raise ValueError(f"未知のボイスバンク種別です: {voicebank.type}")

        loader(engine, voicebank)
        self._loaded_into[id(engine)] = voicebank_id

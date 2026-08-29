"""aural Studio - 音声合成のバックグラウンドワーカー

台本の各行を、話者に設定されたvoice_sourceに応じて2つの経路に振り分けて
合成する:

  - voice_source == "openjtalk": 素の読み上げ。pyopenjtalk.tts()を直接
    呼ぶ(synthesize.synthesize_narration)。VO-SE(vose_core)は経由しない。
  - それ以外: UTAU形式ボイスバンクの識別子とみなし、VO-SE経由で合成する
    (synthesize.synthesize_text)。

台本が全てナレーションのみで構成されている場合は、VoseEngine(vose_core.so
のctypesラッパー)のロード自体が一度も発生しない(遅延ロード)。

合成処理(特にVO-SE経由、および初回のpyopenjtalk辞書ロード)は数秒かかる
ことがあるため、メインスレッド(GUI)で直接呼ぶと画面がフリーズする。
そのためQThreadのサブクラスとしてrun()をオーバーライドし、専用スレッド上で
1行ずつ合成、完了するたびにシグナルで結果(生成したwavファイルのパス)を
GUI側へ通知する。

[設計メモ] QObject+moveToThread()ではなくQThreadを直接継承する理由:
moveToThread()パターンだと、QThreadはデフォルトでrun()内でexec()を呼び
イベントループを回すため、ワーカー(QObject)のrun()完了後もスレッド自体は
イベントループが動き続けている。thread.quit()でイベントループの終了を
「要求」してから実際にスレッドが終わるまでに非同期の間隙があり、
そのタイミングでQThreadオブジェクトの参照を切ってGC対象にしてしまうと、
「QThread: Destroyed while thread is still running」で異常終了する
(実際にこの構成で検証中に踏んだ)。
run()を直接オーバーライドすれば、exec()は呼ばれず、run()から関数が
returnした時点でOSスレッドも終了するため、この種の競合が構造的に
起こらない。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from synthesize import synthesize_narration, synthesize_text
from vose_engine import VoseEngine

NARRATION_VOICE_SOURCE = "openjtalk"


@dataclass
class SynthesisLine:
    line_id: str
    text: str
    voice_source: str = NARRATION_VOICE_SOURCE


class VoseSynthesisThread(QThread):
    """台本の各行を、voice_sourceに応じてナレーション/VO-SEへ振り分けて
    順番に合成するワーカースレッド。
    """

    line_ready = Signal(str, str)   # (line_id, wav_path)
    line_failed = Signal(str, str)  # (line_id, error_message)
    all_finished = Signal()

    def __init__(self, lib_path: str, lines: list[SynthesisLine], parent=None) -> None:
        super().__init__(parent)
        self._lib_path = lib_path
        self._lines = lines
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="aural_vose_"))
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        """このスレッド上で実行される。ここでのシグナル発信はQtが自動的に
        受信側スレッド(通常はメインスレッド)へキュー経由で届けてくれるため、
        直接GUIを操作しない限り安全。
        """
        # VO-SEエンジンはUTAU音源を使う行が1つでもある場合のみ遅延ロードする。
        # 台本が全てナレーション(openjtalk)のみなら、vose_core.soへの依存は
        # 一切発生しない。
        vose_engine: VoseEngine | None = None
        needs_vose = any(line.voice_source != NARRATION_VOICE_SOURCE for line in self._lines)

        if needs_vose:
            try:
                vose_engine = VoseEngine(self._lib_path)
            except Exception as e:  # noqa: BLE001 (GUIへエラー表示するのが目的)
                for line in self._lines:
                    if line.voice_source != NARRATION_VOICE_SOURCE:
                        self.line_failed.emit(line.line_id, f"VO-SEエンジンのロードに失敗: {e}")

        for line in self._lines:
            if self._cancelled:
                break

            wav_path = self._tmp_dir / f"{line.line_id}.wav"
            try:
                if line.voice_source == NARRATION_VOICE_SOURCE:
                    synthesize_narration(line.text, str(wav_path))
                else:
                    if vose_engine is None:
                        # 上のVO-SEロード失敗が既にline_failedで通知済みなのでスキップ
                        continue
                    synthesize_text(vose_engine, line.text, str(wav_path))
                self.line_ready.emit(line.line_id, str(wav_path))
            except Exception as e:  # noqa: BLE001 (1行失敗しても他行は続行する)
                self.line_failed.emit(line.line_id, str(e))

        self.all_finished.emit()


def start_synthesis(
    lib_path: str,
    lines: list[SynthesisLine],
    on_line_ready,
    on_line_failed,
    on_all_finished,
) -> VoseSynthesisThread:
    """合成スレッドを起動するヘルパー。

    呼び出し側は戻り値のスレッドオブジェクトを、スレッドが終了するまで
    (all_finishedが呼ばれ、かつwait()を済ませるまで)参照を保持し続けること。
    """
    thread = VoseSynthesisThread(lib_path, lines)
    thread.line_ready.connect(on_line_ready)
    thread.line_failed.connect(on_line_failed)
    thread.all_finished.connect(on_all_finished)
    thread.start()
    return thread

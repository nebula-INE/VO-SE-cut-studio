"""aural Studio - VO-SE音声合成のバックグラウンドワーカー

VO-SEでの音声合成(execute_render)は数秒かかることがあり(初回は特に
pyopenjtalkの辞書ロードも重なる)、メインスレッド(GUI)で直接呼ぶと
画面がフリーズする。そのためQThreadのサブクラスとしてrun()をオーバー
ライドし、専用スレッド上で1行ずつ合成、完了するたびにシグナルで結果
(生成したwavファイルのパス)をGUI側へ通知する。

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
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from synthesize import synthesize_text
from vose_engine import VoseEngine


class VoseSynthesisThread(QThread):
    """台本の各行を順番に音声合成するワーカースレッド。"""

    line_ready = Signal(str, str)   # (line_id, wav_path)
    line_failed = Signal(str, str)  # (line_id, error_message)
    all_finished = Signal()

    def __init__(self, lib_path: str, lines: list[tuple[str, str]], parent=None) -> None:
        """lines: [(line_id, text), ...] の合成対象リスト。"""
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
        try:
            engine = VoseEngine(self._lib_path)
        except Exception as e:  # noqa: BLE001 (GUIへエラー表示するのが目的)
            for line_id, _text in self._lines:
                self.line_failed.emit(line_id, f"エンジンのロードに失敗: {e}")
            self.all_finished.emit()
            return

        for line_id, text in self._lines:
            if self._cancelled:
                break
            wav_path = self._tmp_dir / f"{line_id}.wav"
            try:
                synthesize_text(engine, text, str(wav_path))
                self.line_ready.emit(line_id, str(wav_path))
            except Exception as e:  # noqa: BLE001 (1行失敗しても他行は続行する)
                self.line_failed.emit(line_id, str(e))

        self.all_finished.emit()


def start_synthesis(
    lib_path: str,
    lines: list[tuple[str, str]],
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

"""aural Studio - メインウィンドウ

映像プレビュー(PreviewPanel)を中央に、台本エディタ(ScriptEditorPanel)を
右側のドッキングパネルとして配置した統合ウィンドウ。

「台本エディタを起点とした動画生成」というコンセプトドキュメント
(plan.md)の方向性に沿って、台本パネルはドッキング(可動・折りたたみ可能)
にして、プレビューを常に主役として中央に据えている。

使い方:
    python3 main_window.py [video_file]
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QMainWindow,
)

from preview import PreviewPanel
from script_editor import ScriptEditorPanel

try:
    from osc_receiver import OscMocapReceiver
    _MOCAP_AVAILABLE = True
except ImportError:
    _MOCAP_AVAILABLE = False


class _MocapBridge(QObject):
    """OscMocapReceiverのコールバックは受信スレッド(Python標準の
    threading.Thread)から呼ばれるため、Qtのシグナル経由でGUIスレッドへ
    安全に中継するための橋渡し役。

    OscMocapReceiver自身はQObjectではない(pythonosc側の都合)ため、
    このクラスを介してsignal/slotの仕組みに乗せている。
    """

    head_transform_received = Signal(float, float, float)  # (offset_x, offset_y, tilt_deg)

    def __init__(self, port: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._receiver = OscMocapReceiver(on_head_transform=self._on_head_transform, port=port)

    def _on_head_transform(self, transform) -> None:
        # 受信スレッドから呼ばれる。Signal.emit()はスレッドセーフなので
        # そのままキュー経由でGUIスレッドへ届く。
        self.head_transform_received.emit(transform.offset_x, transform.offset_y, transform.tilt_deg)

    def start(self) -> None:
        self._receiver.start()

    def stop(self) -> None:
        self._receiver.stop()


def _default_vose_lib_path() -> str | None:
    """CMakeビルドで生成されるlibvose_core(.so/.dylib/.dll)を、このファイルから
    見て ../build/ 相対で探す。見つからなければNoneを返す(音声合成ボタンは
    無効化された状態でGUIは起動できる)。
    """
    system = platform.system()
    filename = {"Windows": "vose_core.dll", "Darwin": "libvose_core.dylib"}.get(system, "libvose_core.so")

    build_dir = Path(__file__).resolve().parent.parent / "build"
    candidate = build_dir / filename
    return str(candidate) if candidate.exists() else None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("aural Studio")
        self.resize(1280, 800)

        self.preview_panel = PreviewPanel()
        self.setCentralWidget(self.preview_panel)

        self.script_panel = ScriptEditorPanel(_default_vose_lib_path())
        self.script_dock = QDockWidget("台本エディタ", self)
        self.script_dock.setWidget(self.script_panel)
        self.script_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.script_dock)

        # 台本エディタの「台本プレビュー再生」で進む現在のセリフを
        # 映像プレビューへテロップとしてオーバーレイ表示する。
        self.script_panel.telop_changed.connect(self.preview_panel.set_telop)
        self.script_panel.mouth_openness_changed.connect(self.preview_panel.set_mouth_openness)

        # モーションキャプチャ(VMCプロトコル/OSC)受信。スマホ等のトラッキング
        # アプリから頭の位置・傾きをリアルタイムに受け取り、映像プレビューの
        # キャラクターへ反映する(plan.md Phase 2)。
        self.mocap_bridge: _MocapBridge | None = None
        if _MOCAP_AVAILABLE:
            self.mocap_bridge = _MocapBridge(port=39539)
            self.mocap_bridge.head_transform_received.connect(self.preview_panel.set_head_transform)
            self.mocap_bridge.start()

        self._build_menu()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("ファイル")

        open_video_action = file_menu.addAction("動画を開く...")
        open_video_action.triggered.connect(self.open_video_dialog)

        file_menu.addSeparator()

        save_script_action = file_menu.addAction("台本を保存...")
        save_script_action.triggered.connect(self.script_panel.save_script)

        open_script_action = file_menu.addAction("台本を開く...")
        open_script_action.triggered.connect(self.script_panel.load_script)

        view_menu = menu_bar.addMenu("表示")
        toggle_script_action = self.script_dock.toggleViewAction()
        toggle_script_action.setText("台本エディタを表示")
        view_menu.addAction(toggle_script_action)

    def open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "動画を開く", "", "動画ファイル (*.mp4 *.mov *.mkv *.avi);;すべてのファイル (*)"
        )
        if path:
            self.preview_panel.open_video(path)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qtの命名規則に合わせる)
        self.preview_panel.cleanup()
        self.script_panel.cleanup()
        if self.mocap_bridge is not None:
            self.mocap_bridge.stop()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()

    # 起動時に動画パスが渡されていればそのまま読み込む
    if len(sys.argv) > 1:
        window.preview_panel.open_video(sys.argv[1])

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

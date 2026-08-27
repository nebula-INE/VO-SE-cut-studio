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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QMainWindow,
)

from preview import PreviewPanel
from script_editor import ScriptEditorPanel


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

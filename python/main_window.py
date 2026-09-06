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
    _OSC_AVAILABLE = True
except ImportError:
    _OSC_AVAILABLE = False

try:
    from websocket_receiver import WebSocketMocapReceiver
    _WEBSOCKET_AVAILABLE = True
except ImportError:
    _WEBSOCKET_AVAILABLE = False

try:
    from preview_3d import Head3DPreviewWidget
    _PREVIEW_3D_AVAILABLE = True
except ImportError:
    _PREVIEW_3D_AVAILABLE = False

_MOCAP_AVAILABLE = _OSC_AVAILABLE or _WEBSOCKET_AVAILABLE


class _MocapBridge(QObject):
    """OSC(VMCプロトコル)・WebSocket(JSON)、どちらの受信経路から来た
    データも、同じQtシグナルに集約して中継する橋渡し役。

    両受信機のコールバックはそれぞれ別スレッド(threading.Thread /
    asyncioイベントループ用スレッド)から呼ばれるため、Qtのシグナル経由で
    GUIスレッドへ安全に中継する。GUI側は、キャラクターがOSC由来か
    WebSocket由来かを区別する必要が無い(同じ形のデータとして届く)。
    """

    head_transform_received = Signal(float, float, float)  # (offset_x, offset_y, tilt_deg) -- 2D用
    head_quaternion_received = Signal(float, float, float, float, float, float, float)
    # (offset_x, offset_y, offset_z, qx, qy, qz, qw) -- 3D用。2D側と同じ受信データから
    # 生成されるが、ロール角だけに単純化していない生のクォータニオンをそのまま運ぶ。
    blend_shapes_received = Signal(dict)  # {ブレンドシェイプ名: 0〜1}

    def __init__(
        self,
        osc_port: int,
        websocket_port: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._osc_receiver = OscMocapReceiver(
            on_head_transform=self._on_head_transform,
            on_blend_shapes=self._on_blend_shapes,
            port=osc_port,
        ) if _OSC_AVAILABLE else None

        self._websocket_receiver = WebSocketMocapReceiver(
            on_head_transform=self._on_head_transform,
            on_blend_shapes=self._on_blend_shapes,
            port=websocket_port,
        ) if _WEBSOCKET_AVAILABLE else None

    def _on_head_transform(self, transform) -> None:
        # 受信スレッドから呼ばれる。Signal.emit()はスレッドセーフなので
        # そのままキュー経由でGUIスレッドへ届く。
        self.head_transform_received.emit(transform.offset_x, transform.offset_y, transform.tilt_deg)
        self.head_quaternion_received.emit(
            transform.offset_x, transform.offset_y, transform.offset_z,
            transform.qx, transform.qy, transform.qz, transform.qw,
        )

    def _on_blend_shapes(self, blend_shapes: dict) -> None:
        self.blend_shapes_received.emit(blend_shapes)

    def start(self) -> None:
        if self._osc_receiver is not None:
            self._osc_receiver.start()
        if self._websocket_receiver is not None:
            self._websocket_receiver.start()

    def stop(self) -> None:
        if self._osc_receiver is not None:
            self._osc_receiver.stop()
        if self._websocket_receiver is not None:
            self._websocket_receiver.stop()


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

        # モーションキャプチャ(VMCプロトコル/OSC・WebSocket)受信。スマホ等の
        # トラッキングアプリから頭の位置・傾きをリアルタイムに受け取り、
        # 映像プレビューのキャラクターへ反映する(plan.md Phase 2)。
        self.mocap_bridge: _MocapBridge | None = None
        if _MOCAP_AVAILABLE:
            self.mocap_bridge = _MocapBridge(osc_port=39539, websocket_port=39540)
            self.mocap_bridge.head_transform_received.connect(self.preview_panel.set_head_transform)
            self.mocap_bridge.blend_shapes_received.connect(self.preview_panel.set_blend_shapes)
            self.mocap_bridge.start()

        # 3Dプレビュー(モード切り替え): 2Dキャラクター(上記)と全く同じ
        # モーションキャプチャデータで、簡易3Dヘッドプロキシを駆動する。
        # 「共通モーションキャプチャエンジンを2D/3D両方に流し込める」ことの
        # 実証用ドック。デフォルトでは非表示(2Dモード相当)。
        self.preview_3d_dock: QDockWidget | None = None
        if _PREVIEW_3D_AVAILABLE:
            self.preview_3d_widget = Head3DPreviewWidget()
            self.preview_3d_dock = QDockWidget("3Dプレビュー(モーションキャプチャ)", self)
            self.preview_3d_dock.setWidget(self.preview_3d_widget)
            self.preview_3d_dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            )
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.preview_3d_dock)
            self.preview_3d_dock.hide()  # デフォルトは2Dモード相当(3Dは非表示)

            if self.mocap_bridge is not None:
                self.mocap_bridge.head_quaternion_received.connect(self.preview_3d_widget.set_head_quaternion)

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

        if self.preview_3d_dock is not None:
            # モード切り替えUI(基礎): 2Dキャラクター(常時表示)に加えて、
            # 同じモーションキャプチャデータで駆動する3Dプレビューの表示/
            # 非表示を切り替えられる。plan.mdの「2Dプロジェクトと3D
            # プロジェクトで動的に最適化される操作画面」の最初の一歩。
            toggle_3d_action = self.preview_3d_dock.toggleViewAction()
            toggle_3d_action.setText("3Dプレビューを表示(モーションキャプチャ)")
            view_menu.addAction(toggle_3d_action)

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

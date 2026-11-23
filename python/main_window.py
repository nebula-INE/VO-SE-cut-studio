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

import inspect
import platform
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMessageBox,
)

from preview import PreviewPanel
from project_model import ProjectDocument, ProjectError
from script_editor import ScriptEditorPanel, ScriptModel

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
    bone_transform_received = Signal(str, float, float, float, float)
    # (ボーン名, qx, qy, qz, qw) -- Head以外のボーン(腕など)。2Dキャラの腕に反映する。

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
            on_bone_transform=self._on_bone_transform,
            port=osc_port,
        ) if _OSC_AVAILABLE else None

        self._websocket_receiver = None
        if _WEBSOCKET_AVAILABLE:
            ws_kwargs = dict(
                on_head_transform=self._on_head_transform,
                on_blend_shapes=self._on_blend_shapes,
                port=websocket_port,
            )
            # WebSocket受信側がボーン通知に対応している場合だけ渡す(未対応の
            # 実装でもTypeErrorで起動が止まらないようにする)。
            if "on_bone_transform" in inspect.signature(WebSocketMocapReceiver.__init__).parameters:
                ws_kwargs["on_bone_transform"] = self._on_bone_transform
            self._websocket_receiver = WebSocketMocapReceiver(**ws_kwargs)

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

    def _on_bone_transform(self, bone) -> None:
        self.bone_transform_received.emit(bone.name, bone.qx, bone.qy, bone.qz, bone.qw)

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
    # MSVCのマルチ構成ジェネレータは build/Release/ 配下に出力する
    for candidate in (build_dir / filename, build_dir / "Release" / filename):
        if candidate.exists():
            return str(candidate)
    return None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("aural Studio")
        self.resize(1280, 800)

        self.preview_panel = PreviewPanel()
        self.setCentralWidget(self.preview_panel)

        self.script_panel = ScriptEditorPanel(_default_vose_lib_path())
        self._project = ProjectDocument.create()
        self._project.script = self.script_panel.model.to_dict()
        self._project_dirty = False
        self.script_panel.content_changed.connect(self._mark_project_dirty)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30_000)
        self._autosave_timer.timeout.connect(self._autosave_project)
        self._autosave_timer.start()

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
        # 音声解析(フォルマント)で推定した母音の口形も反映する。
        self.script_panel.vowels_changed.connect(self.preview_panel.set_vowels)

        # モーションキャプチャ(VMCプロトコル/OSC・WebSocket)受信。スマホ等の
        # トラッキングアプリから頭の位置・傾きをリアルタイムに受け取り、
        # 映像プレビューのキャラクターへ反映する(plan.md Phase 2)。
        self.mocap_bridge: _MocapBridge | None = None
        if _MOCAP_AVAILABLE:
            self.mocap_bridge = _MocapBridge(osc_port=39539, websocket_port=39540)
            self.mocap_bridge.head_transform_received.connect(self.preview_panel.set_head_transform)
            self.mocap_bridge.blend_shapes_received.connect(self.preview_panel.set_blend_shapes)
            self.mocap_bridge.bone_transform_received.connect(self.preview_panel.set_bone_transform)
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
        self._refresh_window_title()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("ファイル")

        new_project_action = file_menu.addAction("新規プロジェクト")
        new_project_action.triggered.connect(self.new_project)
        open_project_action = file_menu.addAction("プロジェクトを開く...")
        open_project_action.triggered.connect(self.open_project)
        save_project_action = file_menu.addAction("プロジェクトを保存")
        save_project_action.triggered.connect(self.save_project)
        save_project_as_action = file_menu.addAction("プロジェクトを別名で保存...")
        save_project_as_action.triggered.connect(self.save_project_as)

        file_menu.addSeparator()
        add_asset_action = file_menu.addAction("素材をプロジェクトに追加...")
        add_asset_action.triggered.connect(self.add_project_asset)
        show_assets_action = file_menu.addAction("プロジェクト素材一覧...")
        show_assets_action.triggered.connect(self.show_project_assets)

        file_menu.addSeparator()
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

            # モード切り替え: 2D(キャラのみ) / 3D(3Dプロキシのみ) / 2D+3D(両方)。
            # どのモードでも同じモーションキャプチャデータで駆動される。
            view_menu.addSeparator()
            mode_menu = view_menu.addMenu("モード")
            mode_group = QActionGroup(self)
            mode_group.setExclusive(True)
            for label, mode in (("2Dモード", "2d"), ("3Dモード", "3d"), ("2D+3D(並べて表示)", "both")):
                action = mode_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(mode == "2d")
                action.triggered.connect(lambda _checked=False, m=mode: self.set_view_mode(m))
                mode_group.addAction(action)

    def _refresh_window_title(self) -> None:
        name = self._project.name or "Untitled"
        dirty_marker = " *" if self._project_dirty else ""
        self.setWindowTitle(f"aural Studio — {name}{dirty_marker}")

    def _mark_project_dirty(self) -> None:
        self._project_dirty = True
        self._refresh_window_title()

    def _sync_project_script(self) -> None:
        self._project.script = self.script_panel.model.to_dict()

    def save_project(self) -> bool:
        if self._project.file_path is None:
            return self.save_project_as()
        self._sync_project_script()
        try:
            saved_path = self._project.save()
        except (OSError, ProjectError) as exc:
            QMessageBox.critical(self, "プロジェクト保存エラー", str(exc))
            return False
        self._project_dirty = False
        self._refresh_window_title()
        self.statusBar().showMessage(f"保存しました: {saved_path}", 5000)
        return True

    def save_project_as(self) -> bool:
        suggested = self._project.name or "Untitled"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "プロジェクトを保存",
            str(Path.home() / f"{suggested}.auralproj"),
            "aural Studio プロジェクト (*.auralproj)",
        )
        if not path:
            return False
        self._sync_project_script()
        try:
            saved_path = self._project.save(path)
        except (OSError, ProjectError) as exc:
            QMessageBox.critical(self, "プロジェクト保存エラー", str(exc))
            return False
        self._project.name = saved_path.stem
        self._project_dirty = False
        self._refresh_window_title()
        self.statusBar().showMessage(f"保存しました: {saved_path}", 5000)
        return True

    def _confirm_discard_changes(self) -> bool:
        if not self._project_dirty:
            return True
        choice = QMessageBox.question(
            self,
            "未保存の変更",
            "プロジェクトに未保存の変更があります。保存しますか？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            return self.save_project()
        if choice == QMessageBox.StandardButton.Discard:
            self._project_dirty = False
            return True
        return False

    def new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        self.script_panel.load_script_data(ScriptModel().to_dict())
        self.preview_panel.close_video()
        self._project = ProjectDocument.create()
        self._project.script = self.script_panel.model.to_dict()
        self._project_dirty = False
        self._refresh_window_title()
        self.statusBar().showMessage("新しいプロジェクトを作成しました", 4000)

    def open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "プロジェクトを開く",
            "",
            "プロジェクト/台本 (*.auralproj *.json);;aural Studio プロジェクト (*.auralproj);;台本 JSON (*.json)",
        )
        if not path:
            return
        try:
            project, recovered = ProjectDocument.load_with_recovery(path)
            if recovered:
                answer = QMessageBox.question(
                    self,
                    "自動保存データの復元",
                    "このプロジェクトには、より新しい自動保存データがあります。復元しますか？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    project = ProjectDocument.load(path)
                    recovered = False
        except (OSError, ProjectError) as exc:
            QMessageBox.critical(self, "プロジェクト読み込みエラー", str(exc))
            return

        if not self.script_panel.load_script_data(project.script):
            return
        self._project = project
        self._project_dirty = recovered
        self._refresh_window_title()

        missing = self._project.missing_assets()
        if missing:
            names = "\n".join(f"• {asset.name} ({asset.path})" for asset in missing[:10])
            extra = "" if len(missing) <= 10 else f"\nほか {len(missing) - 10} 件"
            QMessageBox.warning(
                self,
                "見つからない素材",
                f"次の素材を見つけられませんでした。素材一覧からパスを確認してください。\n{names}{extra}",
            )
        self.statusBar().showMessage(f"開きました: {path}", 5000)

    def _guess_asset_kind(self, path: str) -> str:
        extension = Path(path).suffix.lower()
        if extension in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
            return "video"
        if extension in {".wav", ".mp3", ".flac", ".aac", ".m4a", ".ogg"}:
            return "audio"
        if extension in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            return "image"
        if extension in {".vrm", ".vmd", ".fbx", ".obj", ".glb", ".gltf"}:
            return "character"
        return "other"

    def _register_linked_asset(self, path: str, kind: str) -> None:
        before = len(self._project.assets)
        try:
            self._project.add_asset(path, kind, copy_into_project=False)
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "素材登録エラー", str(exc))
            return
        if len(self._project.assets) != before:
            self._mark_project_dirty()

    def add_project_asset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "プロジェクトに追加する素材を選択",
            "",
            "メディア/素材 (*.mp4 *.mov *.mkv *.avi *.webm *.wav *.mp3 *.flac *.aac *.m4a *.ogg *.png *.jpg *.jpeg *.webp *.bmp *.vrm *.vmd *.fbx *.obj *.glb *.gltf);;すべてのファイル (*)",
        )
        if not path:
            return

        choice_box = QMessageBox(self)
        choice_box.setWindowTitle("素材の保存方法")
        choice_box.setText("素材をプロジェクト内にコピーしますか？")
        choice_box.setInformativeText(
            "コピーするとプロジェクトを移動しても素材を引き継げます。"
            "外部参照を選ぶと元ファイルをその場所に残します。"
        )
        copy_button = choice_box.addButton("プロジェクトにコピー", QMessageBox.ButtonRole.AcceptRole)
        link_button = choice_box.addButton("外部ファイルを参照", QMessageBox.ButtonRole.ActionRole)
        choice_box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        choice_box.exec()
        clicked = choice_box.clickedButton()
        if clicked not in (copy_button, link_button):
            return

        copy_into_project = clicked is copy_button
        if copy_into_project and self._project.file_path is None:
            if not self.save_project_as():
                return
        try:
            asset = self._project.add_asset(
                path,
                self._guess_asset_kind(path),
                copy_into_project=copy_into_project,
            )
        except (OSError, ProjectError) as exc:
            QMessageBox.critical(self, "素材追加エラー", str(exc))
            return
        self._mark_project_dirty()
        self.statusBar().showMessage(f"素材を追加しました: {asset.name}", 5000)

    def show_project_assets(self) -> None:
        if not self._project.assets:
            QMessageBox.information(self, "プロジェクト素材", "登録されている素材はありません。")
            return
        lines = []
        for asset in self._project.assets:
            try:
                exists = self._project.resolve_asset(asset).exists()
            except (OSError, ProjectError):
                exists = False
            state = "OK" if exists else "見つかりません"
            storage = "プロジェクト内" if asset.managed else "外部参照"
            lines.append(f"[{state}] {asset.kind} / {asset.name} — {storage}\n{asset.path}")
        QMessageBox.information(self, "プロジェクト素材", "\n\n".join(lines))

    def _autosave_project(self) -> None:
        if not self._project_dirty or self._project.file_path is None:
            return
        self._sync_project_script()
        try:
            path = self._project.save_autosave()
        except (OSError, ProjectError) as exc:
            self.statusBar().showMessage(f"自動保存に失敗しました: {exc}", 8000)
            return
        self.statusBar().showMessage(f"自動保存しました: {path}", 3000)

    def set_view_mode(self, mode: str) -> None:
        """表示モードを切り替える。mode: "2d" | "3d" | "both"。"""
        show_2d = mode in ("2d", "both")
        show_3d = mode in ("3d", "both")
        self.preview_panel.set_character_visible(show_2d)
        if self.preview_3d_dock is not None:
            self.preview_3d_dock.setVisible(show_3d)

    def open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "動画を開く", "", "動画ファイル (*.mp4 *.mov *.mkv *.avi);;すべてのファイル (*)"
        )
        if path and self.preview_panel.open_video(path):
            self._register_linked_asset(path, "video")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qtの命名規則に合わせる)
        if not self._confirm_discard_changes():
            event.ignore()
            return
        self._autosave_timer.stop()
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
        startup_video = sys.argv[1]
        if window.preview_panel.open_video(startup_video):
            window._register_linked_asset(startup_video, "video")

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

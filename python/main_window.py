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
import wave
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QInputDialog,
)

from preview import PreviewPanel
from project_model import ProjectDocument, ProjectError
from script_editor import ScriptEditorPanel, ScriptModel
from timeline_editor import TimelineEditorWidget
from timeline_model import TimelineError, TimelineModel

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
        self._discard_pending = False
        self.script_panel.content_changed.connect(self._mark_project_dirty)
        self.script_panel.asset_imported.connect(self._register_linked_asset)

        self.timeline_panel = TimelineEditorWidget(TimelineModel.create_default())
        self._active_timeline_video_clip_id: str | None = None
        self.timeline_panel.changed.connect(self._on_timeline_edited)
        self.timeline_panel.playhead_changed.connect(self._on_timeline_playhead_changed)
        self.timeline_panel.canvas.selection_changed.connect(self._on_timeline_selection_changed)
        self.preview_panel.current_time_changed.connect(self._on_video_time_changed)
        self.timeline_panel.add_clip_requested.connect(self.add_timeline_clip_from_assets)
        self.timeline_panel.add_track_requested.connect(self.add_timeline_track)

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

        self.timeline_dock = QDockWidget("タイムライン", self)
        self.timeline_dock.setWidget(self.timeline_panel)
        self.timeline_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.timeline_dock)

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
        remove_asset_action = file_menu.addAction("素材参照を削除...")
        remove_asset_action.triggered.connect(self.remove_project_asset)

        file_menu.addSeparator()
        open_video_action = file_menu.addAction("動画を開く...")
        open_video_action.triggered.connect(self.open_video_dialog)

        file_menu.addSeparator()
        save_script_action = file_menu.addAction("台本を保存...")
        save_script_action.triggered.connect(self.script_panel.save_script)
        open_script_action = file_menu.addAction("台本を開く...")
        open_script_action.triggered.connect(self.open_script)

        timeline_menu = menu_bar.addMenu("タイムライン")
        add_clip_action = timeline_menu.addAction("プロジェクト素材からクリップを追加...")
        add_clip_action.triggered.connect(self.add_timeline_clip_from_assets)
        add_track_action = timeline_menu.addAction("トラックを追加...")
        add_track_action.triggered.connect(self.add_timeline_track)
        timeline_menu.addSeparator()
        split_clip_action = timeline_menu.addAction("選択クリップを再生ヘッド位置で分割")
        split_clip_action.triggered.connect(self.timeline_panel.split_selected_at_playhead)
        delete_clip_action = timeline_menu.addAction("選択クリップを削除")
        delete_clip_action.triggered.connect(self.timeline_panel.delete_selected)
        timeline_menu.addSeparator()
        caption_action = timeline_menu.addAction("台本字幕をタイムラインに配置")
        caption_action.triggered.connect(self.add_script_lines_to_timeline)

        view_menu = menu_bar.addMenu("表示")
        toggle_script_action = self.script_dock.toggleViewAction()
        toggle_script_action.setText("台本エディタを表示")
        view_menu.addAction(toggle_script_action)
        toggle_timeline_action = self.timeline_dock.toggleViewAction()
        toggle_timeline_action.setText("タイムラインを表示")
        view_menu.addAction(toggle_timeline_action)

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
        self._project.timeline = self.timeline_panel.model.to_dict()

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
        previous_name = self._project.name
        self._project.name = Path(path).stem or previous_name
        try:
            saved_path = self._project.save(path)
        except (OSError, ProjectError) as exc:
            self._project.name = previous_name
            QMessageBox.critical(self, "プロジェクト保存エラー", str(exc))
            return False
        self._project_dirty = False
        self._refresh_window_title()
        self.statusBar().showMessage(f"保存しました: {saved_path}", 5000)
        return True

    @staticmethod
    def _remove_autosave_file(path: Path | None) -> None:
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def _confirm_discard_changes(self) -> bool:
        self._discard_pending = False
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
            # Do not clear the dirty flag or remove autosave until the requested
            # replacement succeeds. The file picker may still be cancelled.
            self._discard_pending = True
            return True
        return False

    def open_script(self) -> None:
        """Load a standalone script without bypassing the project's dirty-state guard."""
        previous_autosave = self._project.autosave_path
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(self, "台本を開く", "", "JSON (*.json)")
        if not path:
            self._discard_pending = False
            return
        if not self.script_panel.load_script_from_path(path):
            self._discard_pending = False
            return
        if self._discard_pending:
            self._remove_autosave_file(previous_autosave)
        self._discard_pending = False

    def new_project(self) -> None:
        previous_autosave = self._project.autosave_path
        if not self._confirm_discard_changes():
            return
        if not self.script_panel.load_script_data(ScriptModel().to_dict()):
            self._discard_pending = False
            return
        self._active_timeline_video_clip_id = None
        self.preview_panel.close_video()
        self.timeline_panel.set_model(TimelineModel.create_default())
        self._project = ProjectDocument.create()
        self._project.script = self.script_panel.model.to_dict()
        self._project.timeline = self.timeline_panel.model.to_dict()
        if self._discard_pending:
            self._remove_autosave_file(previous_autosave)
        self._discard_pending = False
        self._project_dirty = False
        self._refresh_window_title()
        self.statusBar().showMessage("新しいプロジェクトを作成しました", 4000)

    def open_project(self) -> None:
        previous_project_path = self._project.file_path
        previous_autosave = self._project.autosave_path
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "プロジェクトを開く",
            "",
            "プロジェクト/台本 (*.auralproj *.json);;aural Studio プロジェクト (*.auralproj);;台本 JSON (*.json)",
        )
        if not path:
            self._discard_pending = False
            return
        try:
            project, recovered = ProjectDocument.load_with_recovery(path)
            timeline_model = TimelineModel.from_dict(project.timeline)
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
                    timeline_model = TimelineModel.from_dict(project.timeline)
                    recovered = False
        except (OSError, ProjectError, TimelineError) as exc:
            self._discard_pending = False
            QMessageBox.critical(self, "プロジェクト読み込みエラー", str(exc))
            return

        if not self.script_panel.load_script_data(project.script):
            self._discard_pending = False
            return

        self._active_timeline_video_clip_id = None
        self.timeline_panel.set_model(timeline_model)
        self._project = project
        self._project_dirty = recovered
        if self._discard_pending:
            # Keep a recovered autosave when reopening that exact project; it is
            # the only copy of changes the user explicitly chose to recover.
            if previous_autosave and (
                previous_project_path != project.file_path or not recovered
            ):
                self._remove_autosave_file(previous_autosave)
        self._discard_pending = False
        self._refresh_window_title()
        self.preview_panel.close_video()

        active_video_id = self._project.settings.get("active_video_asset_id")
        active_video = self._project.get_asset(active_video_id) if isinstance(active_video_id, str) else None
        if active_video is not None:
            try:
                active_video_path = self._project.resolve_asset(active_video)
                if active_video_path.is_file():
                    self.preview_panel.open_video(str(active_video_path))
                    saved_clip_id = self._project.settings.get("active_timeline_video_clip_id")
                    active_clip = (
                        timeline_model.get_clip(saved_clip_id)
                        if isinstance(saved_clip_id, str)
                        else None
                    )
                    if (
                        active_clip is None
                        or active_clip.kind != "video"
                        or active_clip.asset_id != active_video.id
                    ):
                        active_clip = next(
                            (
                                clip for clip in sorted(
                                    timeline_model.clips, key=lambda item: (item.start, item.id)
                                )
                                if clip.kind == "video" and clip.asset_id == active_video.id
                            ),
                            None,
                        )
                    if active_clip is not None:
                        self._active_timeline_video_clip_id = active_clip.id
                        self.timeline_panel.canvas._set_selected_clip(active_clip.id)
                        self.timeline_panel.set_playhead(active_clip.start)
                        self.preview_panel.seek_to(active_clip.source_in)
            except (OSError, ProjectError):
                pass

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

    def _video_clip_at(self, timeline_seconds: float):
        candidates = [
            clip for clip in self.timeline_panel.model.clips
            if clip.kind == "video" and clip.start <= timeline_seconds < clip.end
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda clip: (clip.start, clip.id))

    def _on_video_time_changed(self, source_seconds: float) -> None:
        """Map source-media timestamps back to the selected clip's timeline time."""
        clip = self.timeline_panel.model.get_clip(self._active_timeline_video_clip_id or "")
        if clip is not None and clip.kind == "video":
            self.timeline_panel.set_playhead(clip.timeline_time_at(source_seconds))
        else:
            self.timeline_panel.set_playhead(source_seconds)

    def _on_timeline_playhead_changed(self, seconds: float) -> None:
        """Seek source media using the clip mapping at the requested timeline time."""
        clip = self._video_clip_at(seconds)
        if clip is None or clip.asset_id is None:
            self._active_timeline_video_clip_id = None
            if self._project.settings.pop("active_timeline_video_clip_id", None) is not None:
                self._mark_project_dirty()
            self.preview_panel.seek_to(seconds)
            return

        asset = self._project.get_asset(clip.asset_id)
        if asset is None:
            self.statusBar().showMessage("動画クリップの素材が見つかりません", 5000)
            return
        try:
            path = self._project.resolve_asset(asset)
            if not path.is_file():
                self.statusBar().showMessage(f"動画素材が見つかりません: {path}", 6000)
                return
            active_id = self._project.settings.get("active_video_asset_id")
            if self.preview_panel.decoder is None or active_id != asset.id:
                if not self.preview_panel.open_video(str(path)):
                    self.statusBar().showMessage(f"動画を開けませんでした: {path}", 6000)
                    return
                self._project.settings["active_video_asset_id"] = asset.id
                self._mark_project_dirty()
            self._active_timeline_video_clip_id = clip.id
            if self._project.settings.get("active_timeline_video_clip_id") != clip.id:
                self._project.settings["active_timeline_video_clip_id"] = clip.id
                self._mark_project_dirty()
            self.preview_panel.seek_to(clip.source_time_at(seconds))
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "動画クリップを開けません", str(exc))

    def _on_timeline_selection_changed(self, clip_id: str) -> None:
        """Selecting a video clip loads its source and jumps to its timeline start."""
        if not clip_id:
            return
        clip = self.timeline_panel.model.get_clip(clip_id)
        if clip is None or clip.kind != "video" or clip.asset_id is None:
            return
        asset = self._project.get_asset(clip.asset_id)
        if asset is None:
            self.statusBar().showMessage("選択した動画素材がプロジェクトに見つかりません", 5000)
            return
        try:
            path = self._project.resolve_asset(asset)
            if not path.is_file():
                self.statusBar().showMessage(f"動画素材が見つかりません: {path}", 6000)
                return
            active_id = self._project.settings.get("active_video_asset_id")
            if self.preview_panel.decoder is None or active_id != asset.id:
                if not self.preview_panel.open_video(str(path)):
                    self.statusBar().showMessage(f"動画を開けませんでした: {path}", 6000)
                    return
                self._project.settings["active_video_asset_id"] = asset.id
                self._mark_project_dirty()
            self._active_timeline_video_clip_id = clip.id
            if self._project.settings.get("active_timeline_video_clip_id") != clip.id:
                self._project.settings["active_timeline_video_clip_id"] = clip.id
                self._mark_project_dirty()
            self.timeline_panel.set_playhead(clip.start)
            self.preview_panel.seek_to(clip.source_in)
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "動画クリップを開けません", str(exc))

    def _on_timeline_edited(self) -> None:
        self._mark_project_dirty()
        clip = self.timeline_panel.model.get_clip(self._active_timeline_video_clip_id or "")
        if clip is None:
            self._active_timeline_video_clip_id = None
            self._project.settings.pop("active_timeline_video_clip_id", None)
            return
        if clip.kind == "video" and self.preview_panel.decoder is not None:
            self.preview_panel.seek_to(clip.source_time_at(self.timeline_panel.playhead_seconds))

    def add_script_lines_to_timeline(self) -> None:
        """Place script lines on an overlay track as linked text clips.

        Existing clips are kept in place; only script lines without a timeline
        clip are appended, making the operation safe to repeat.
        """
        lines = self.script_panel.model.lines
        if not lines:
            QMessageBox.information(self, "台本字幕", "台本にセリフがありません。")
            return
        existing_ids = {
            clip.script_line_id
            for clip in self.timeline_panel.model.clips
            if clip.script_line_id is not None
        }
        tracks = [
            track for track in self.timeline_panel.model.tracks
            if track.kind == "overlay" and not track.locked
        ]
        if not tracks:
            index = 1 + sum(
                1 for track in self.timeline_panel.model.tracks if track.kind == "overlay"
            )
            tracks = [
                self.timeline_panel.model.add_track(f"Overlay {index}", "overlay")
            ]

        elapsed = 0.0
        added = 0
        for line in lines:
            duration = max(0.05, float(line.duration_sec))
            if line.line_id not in existing_ids:
                try:
                    self.timeline_panel.model.add_clip(
                        tracks[0].id,
                        "text",
                        line.text or "（空の字幕）",
                        duration,
                        start=elapsed,
                        script_line_id=line.line_id,
                    )
                except TimelineError as exc:
                    QMessageBox.warning(self, "台本字幕を配置できません", str(exc))
                    break
                added += 1
            elapsed += duration

        if added:
            self.timeline_panel.refresh()
            self._mark_project_dirty()
            self.statusBar().showMessage(f"台本字幕を {added} 件タイムラインに配置しました", 5000)
        else:
            QMessageBox.information(self, "台本字幕", "すべてのセリフはすでに配置済みです。")

    def add_timeline_track(self) -> None:
        labels = ["Video", "Audio", "Overlay", "Motion", "Other"]
        kinds = ["video", "audio", "overlay", "motion", "other"]
        selected, accepted = QInputDialog.getItem(
            self, "トラックを追加", "トラック種別:", labels, 0, False
        )
        if not accepted:
            return
        kind = kinds[labels.index(selected)]
        count = 1 + sum(1 for track in self.timeline_panel.model.tracks if track.kind == kind)
        name = f"{selected} {count}"
        try:
            self.timeline_panel.model.add_track(name, kind)
            self.timeline_panel.refresh()
            self._mark_project_dirty()
        except TimelineError as exc:
            QMessageBox.warning(self, "トラック追加エラー", str(exc))

    def _asset_default_duration(self, asset) -> float:
        if asset.kind == "video":
            active_id = self._project.settings.get("active_video_asset_id")
            if active_id == asset.id and self.preview_panel.decoder is not None:
                duration = float(self.preview_panel.decoder.duration_seconds)
                if duration >= 0.05:
                    return duration
            return 5.0
        if asset.kind == "audio":
            try:
                path = self._project.resolve_asset(asset)
                with wave.open(str(path), "rb") as audio:
                    duration = audio.getnframes() / float(audio.getframerate())
                if duration >= 0.05:
                    return duration
            except (OSError, wave.Error, ProjectError, ZeroDivisionError):
                pass
            return 5.0
        return 5.0

    def add_timeline_clip_from_assets(self) -> None:
        available = [
            asset for asset in self._project.assets
            if asset.kind in {"video", "audio", "image", "character", "other"}
        ]
        if not available:
            QMessageBox.information(
                self, "クリップを追加", "先に「ファイル → 素材をプロジェクトに追加」で素材を登録してください。"
            )
            return
        labels = [
            f"{index + 1}. {asset.name} [{asset.kind}]"
            for index, asset in enumerate(available)
        ]
        selected, accepted = QInputDialog.getItem(
            self, "クリップを追加", "使用する素材:", labels, 0, False
        )
        if not accepted:
            return
        asset = available[labels.index(selected)]

        mapping = {
            "video": ("video", "video"),
            "audio": ("audio", "audio"),
            "image": ("image", "overlay"),
            "character": ("character", "overlay"),
            "other": ("other", "other"),
        }
        clip_kind, track_kind = mapping[asset.kind]
        tracks = [
            track for track in self.timeline_panel.model.tracks
            if track.kind == track_kind and not track.locked
        ]
        if not tracks:
            default_name = {
                "video": "Video 1",
                "audio": "Audio 1",
                "overlay": "Overlay 1",
                "other": "Other 1",
            }.get(track_kind, f"{track_kind.title()} 1")
            suffix = 1 + sum(
                1 for track in self.timeline_panel.model.tracks if track.kind == track_kind
            )
            tracks = [self.timeline_panel.model.add_track(f"{default_name.split()[0]} {suffix}", track_kind)]

        default_duration = self._asset_default_duration(asset)
        duration, accepted = QInputDialog.getDouble(
            self,
            "クリップの長さ",
            "タイムライン上の長さ (秒):",
            default_duration,
            0.05,
            3600.0,
            2,
        )
        if not accepted:
            return

        try:
            self.timeline_panel.add_clip(
                tracks[0].id,
                clip_kind,
                asset.name,
                duration,
                asset_id=asset.id,
            )
        except TimelineError as exc:
            QMessageBox.warning(self, "クリップ追加エラー", str(exc))
            return
        self.statusBar().showMessage(f"タイムラインに追加しました: {asset.name}", 5000)

    def _add_video_asset_to_timeline(self, asset) -> None:
        if any(clip.asset_id == asset.id for clip in self.timeline_panel.model.clips):
            return
        tracks = [
            track for track in self.timeline_panel.model.tracks
            if track.kind == "video" and not track.locked
        ]
        if not tracks:
            tracks = [self.timeline_panel.model.add_track("Video 1", "video")]
        duration = self._asset_default_duration(asset)
        try:
            self.timeline_panel.add_clip(
                tracks[0].id,
                "video",
                asset.name,
                duration,
                start=0.0,
                asset_id=asset.id,
            )
        except TimelineError as exc:
            QMessageBox.warning(self, "タイムライン追加エラー", str(exc))

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

    def _register_linked_asset(self, path: str, kind: str):
        before = len(self._project.assets)
        try:
            asset = self._project.add_asset(path, kind, copy_into_project=False)
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "素材登録エラー", str(exc))
            return None
        if len(self._project.assets) != before:
            self._mark_project_dirty()
        return asset

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

    def remove_project_asset(self) -> None:
        if not self._project.assets:
            QMessageBox.information(self, "素材参照の削除", "削除できる素材はありません。")
            return
        labels = [
            f"{index + 1}. {asset.name} [{asset.kind}]"
            for index, asset in enumerate(self._project.assets)
        ]
        selected, accepted = QInputDialog.getItem(
            self, "素材参照の削除", "削除する素材:", labels, 0, False
        )
        if not accepted:
            return
        index = labels.index(selected)
        asset = self._project.assets[index]
        delete_file = False
        if asset.managed:
            answer = QMessageBox.question(
                self,
                "プロジェクト内の素材",
                f"「{asset.name}」のファイルもプロジェクトから削除しますか？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            delete_file = answer == QMessageBox.StandardButton.Yes
        try:
            self._project.remove_asset(asset.id, delete_managed_file=delete_file)
        except (OSError, ProjectError) as exc:
            QMessageBox.critical(self, "素材参照の削除エラー", str(exc))
            return
        self._mark_project_dirty()
        self.statusBar().showMessage(f"素材参照を削除しました: {asset.name}", 5000)

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
            asset = self._register_linked_asset(path, "video")
            if asset is not None:
                self._project.settings["active_video_asset_id"] = asset.id
                self._add_video_asset_to_timeline(asset)
                self._mark_project_dirty()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qtの命名規則に合わせる)
        if not self._confirm_discard_changes():
            event.ignore()
            return
        if self._discard_pending:
            self._remove_autosave_file(self._project.autosave_path)
            self._discard_pending = False
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
            asset = window._register_linked_asset(startup_video, "video")
            if asset is not None:
                window._project.settings["active_video_asset_id"] = asset.id
                window._add_video_asset_to_timeline(asset)
                window._mark_project_dirty()

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

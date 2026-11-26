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
import math
import platform
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QRunnable, QThreadPool
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
from runtime_paths import find_runtime_file
from project_model import ProjectDocument, ProjectError
from script_editor import ScriptEditorPanel, ScriptModel
from timeline_editor import TimelineEditorWidget
from timeline_model import TimelineError, TimelineModel
from timeline_audio import TimelineAudioPlayback
from timeline_audio_render import (
    render_timeline_audio,
    timeline_audio_clips,
)
from media_ffmpeg import (
    MediaToolError,
    find_ffmpeg_executable,
    find_ffprobe_executable,
    mux_audio_into_video,
)
from timeline_video_render import (
    BUILTIN_2D_CHARACTER_ID,
    VideoRenderError,
    render_timeline_video,
    timeline_motion_clips,
    timeline_video_clips,
)
from character_motion import (
    CharacterMotionError,
    load_motion_file,
    sample_motion,
    save_motion_file,
)
from vrm_mocap import (
    motion_state_to_vrm_pose,
    should_add_character_clip_for_recording,
)
from motion_capture_recorder import (
    FRAME_RATE as MOTION_CAPTURE_FRAME_RATE,
    MAX_RECORDING_SECONDS,
    MotionCaptureRecorder,
    MotionCaptureRecordingError,
)

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
    from vrm_preview import VRMViewerWidget
    _VRM_VIEWER_AVAILABLE = True
except ImportError:
    _VRM_VIEWER_AVAILABLE = False

try:
    from preview_3d import Head3DPreviewWidget
    _HEAD3D_PREVIEW_AVAILABLE = True
except ImportError:
    _HEAD3D_PREVIEW_AVAILABLE = False

_PREVIEW_3D_AVAILABLE = _VRM_VIEWER_AVAILABLE or _HEAD3D_PREVIEW_AVAILABLE

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
    # (ボーン名, qx, qy, qz, qw) -- 既存2Dレンダラー向け。シグネチャは維持する。
    bone_transform_3d_received = Signal(
        str, float, float, float, float, float, float, float
    )
    # (ボーン名, x, y, z, qx, qy, qz, qw) -- VRM用に元の位置も保持する。

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
        self.bone_transform_3d_received.emit(
            bone.name, bone.x, bone.y, bone.z,
            bone.qx, bone.qy, bone.qz, bone.qw,
        )

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
    """Find VO-SE's native library in a frozen bundle or a source build tree."""
    system = platform.system()
    if system == "Windows":
        filenames = ("vose_core.dll", "libvose_core.dll")
    elif system == "Darwin":
        filenames = ("libvose_core.dylib",)
    else:
        filenames = ("libvose_core.so",)

    candidate = find_runtime_file(
        filenames,
        module_file=__file__,
        executable=sys.executable,
        frozen_root=getattr(sys, "_MEIPASS", None),
        subdirectories=("native", "build", "build/Release"),
    )
    return str(candidate) if candidate is not None else None


class _TimelineAudioRenderSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class _TimelineAudioRenderTask(QRunnable):
    """Run a mixdown against a frozen timeline/asset-path snapshot off the GUI thread."""

    def __init__(
        self,
        timeline_payload: dict,
        asset_paths: dict[str, str],
        output_path: str,
    ) -> None:
        super().__init__()
        self.timeline_payload = timeline_payload
        self.asset_paths = asset_paths
        self.output_path = output_path
        self.signals = _TimelineAudioRenderSignals()

    def run(self) -> None:
        try:
            model = TimelineModel.from_dict(self.timeline_payload)
            result = render_timeline_audio(
                model,
                lambda clip: self.asset_paths[clip.id],
                self.output_path,
            )
        except Exception as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.finished.emit(result)





class _TimelineAudioVideoExportTask(QRunnable):
    """Render the timeline mix and mux it into an existing composed video."""

    def __init__(
        self,
        timeline_payload: dict,
        asset_paths: dict[str, str],
        video_path: str,
        output_path: str,
        ffmpeg_path: str,
    ) -> None:
        super().__init__()
        self.timeline_payload = timeline_payload
        self.asset_paths = asset_paths
        self.video_path = video_path
        self.output_path = output_path
        self.ffmpeg_path = ffmpeg_path
        self.signals = _TimelineAudioRenderSignals()

    def run(self) -> None:
        try:
            model = TimelineModel.from_dict(self.timeline_payload)
            with tempfile.TemporaryDirectory(prefix="aural-video-export-") as work_dir:
                mix_path = Path(work_dir) / "timeline_audio_mix.wav"
                mix_result = render_timeline_audio(
                    model,
                    lambda clip: self.asset_paths[clip.id],
                    mix_path,
                    ffmpeg_path=self.ffmpeg_path,
                )
                mux_result = mux_audio_into_video(
                    self.video_path,
                    mix_result.output_path,
                    self.output_path,
                    ffmpeg_path=self.ffmpeg_path,
                )
            self.signals.finished.emit({
                "output_path": str(mux_result.output_path),
                "audio_clip_count": mix_result.audio_clip_count,
                "duration_seconds": mix_result.duration_seconds,
                "clipped_sample_count": mix_result.clipped_sample_count,
                "peak_level": mix_result.peak_level,
            })
        except Exception as exc:
            self.signals.failed.emit(str(exc))



class _TimelineFullVideoExportTask(QRunnable):
    """Render all supported visual clips and optionally add the timeline audio mix."""

    def __init__(
        self,
        timeline_payload: dict,
        asset_paths: dict[str, str],
        output_path: str,
        ffmpeg_path: str,
        subtitle_texts: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.timeline_payload = timeline_payload
        self.asset_paths = asset_paths
        self.output_path = output_path
        self.ffmpeg_path = ffmpeg_path
        self.subtitle_texts = dict(subtitle_texts or {})
        self.signals = _TimelineAudioRenderSignals()

    def run(self) -> None:
        try:
            model = TimelineModel.from_dict(self.timeline_payload)
            visual_clips = timeline_video_clips(model, self.subtitle_texts)
            motion_clips = timeline_motion_clips(model)
            audio_clips = timeline_audio_clips(model)
            duration = max(
                [clip.end for clip in visual_clips]
                + [clip.end for clip in audio_clips]
            )
            with tempfile.TemporaryDirectory(prefix="aural-timeline-video-") as work_dir:
                silent_video_path = (
                    Path(work_dir) / "timeline_video.mp4"
                    if audio_clips else Path(self.output_path)
                )
                video_result = render_timeline_video(
                    model,
                    lambda clip: self.asset_paths[clip.id],
                    silent_video_path,
                    duration_seconds=duration,
                    ffmpeg_path=self.ffmpeg_path,
                    subtitle_texts=self.subtitle_texts,
                    motion_clips=motion_clips,
                    voice_clips=audio_clips,
                )
                clipped_sample_count = 0
                peak_level = 0.0
                if audio_clips:
                    audio_path = Path(work_dir) / "timeline_audio.wav"
                    audio_result = render_timeline_audio(
                        model,
                        lambda clip: self.asset_paths[clip.id],
                        audio_path,
                        duration_seconds=duration,
                        ffmpeg_path=self.ffmpeg_path,
                    )
                    mux_audio_into_video(
                        video_result.output_path,
                        audio_result.output_path,
                        self.output_path,
                        ffmpeg_path=self.ffmpeg_path,
                    )
                    clipped_sample_count = audio_result.clipped_sample_count
                    peak_level = audio_result.peak_level
            self.signals.finished.emit({
                "output_path": str(Path(self.output_path).expanduser().resolve()),
                "video_clip_count": video_result.video_clip_count,
                "subtitle_clip_count": video_result.subtitle_clip_count,
                "character_clip_count": video_result.character_clip_count,
                "motion_clip_count": video_result.motion_clip_count,
                "audio_clip_count": len(audio_clips),
                "duration_seconds": video_result.duration_seconds,
                "width": video_result.width,
                "height": video_result.height,
                "frame_rate": video_result.frame_rate,
                "clipped_sample_count": clipped_sample_count,
                "peak_level": peak_level,
            })
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Axral Cut Studio")
        self.resize(1280, 800)

        self.preview_panel = PreviewPanel()
        self.preview_panel.loop_playback = False
        self.setCentralWidget(self.preview_panel)

        self.script_panel = ScriptEditorPanel(_default_vose_lib_path())
        self._project = ProjectDocument.create()
        self._project.script = self.script_panel.model.to_dict()
        self._pending_generated_audio: dict[str, tuple[str, str]] = {}
        self._motion_capture_recorder = MotionCaptureRecorder()
        self._vrm_motion_cache: dict[str, tuple[str, int, list[dict]]] = {}
        self._motion_capture_timer = QTimer(self)
        self._motion_capture_timer.setInterval(round(1000 / MOTION_CAPTURE_FRAME_RATE))
        self._motion_capture_timer.timeout.connect(self._capture_motion_frame)
        self._motion_capture_start_action = None
        self._motion_capture_stop_action = None
        self._project_dirty = False
        self._discard_pending = False
        self.script_panel.content_changed.connect(self._mark_project_dirty)
        self.script_panel.asset_imported.connect(self._register_linked_asset)
        self.script_panel.synth_audio_ready.connect(self._on_synth_audio_ready)
        self.script_panel.synth_audio_batch_finished.connect(self._persist_pending_generated_audio)

        self.timeline_panel = TimelineEditorWidget(TimelineModel.create_default())
        self.timeline_audio = TimelineAudioPlayback(
            lambda: self.timeline_panel.model,
            self._resolve_timeline_audio_asset,
            self,
        )
        self.timeline_audio.position_changed.connect(self._on_audio_timeline_position_changed)
        self.timeline_audio.error.connect(
            lambda message: self.statusBar().showMessage(message, 8000)
        )
        self._audio_render_pool = QThreadPool(self)
        self._audio_render_pool.setMaxThreadCount(1)
        self._audio_render_task: _TimelineAudioRenderTask | None = None
        self._audio_video_export_task: _TimelineAudioVideoExportTask | None = None
        self._timeline_video_export_task: _TimelineFullVideoExportTask | None = None
        self._export_audio_mix_action = None
        self._export_audio_video_action = None
        self._export_timeline_video_action = None
        self._active_timeline_video_clip_id: str | None = None
        self._timeline_playback_requested = False
        self._timeline_playback_transition = False
        self._timeline_playback_generation = 0
        self._pending_timeline_clip_id: str | None = None
        self.timeline_panel.changed.connect(self._on_timeline_edited)
        self.timeline_panel.playhead_changed.connect(self._on_timeline_playhead_changed)
        self.timeline_panel.playback_toggle_requested.connect(self.toggle_timeline_playback)
        self.timeline_panel.playback_stop_requested.connect(self.stop_timeline_playback)
        self.timeline_panel.canvas.selection_changed.connect(self._on_timeline_selection_changed)
        self.preview_panel.current_time_changed.connect(self._on_video_time_changed)
        self.preview_panel.playback_state_changed.connect(self._on_preview_playback_state_changed)
        self.preview_panel.playback_finished.connect(self._on_preview_playback_finished)
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
            self.mocap_bridge.head_transform_received.connect(self._record_motion_head)
            self.mocap_bridge.head_quaternion_received.connect(self._record_motion_head_3d)
            self.mocap_bridge.blend_shapes_received.connect(self._record_motion_blend_shapes)
            self.mocap_bridge.bone_transform_received.connect(self._record_motion_bone)
            self.mocap_bridge.bone_transform_3d_received.connect(self._record_motion_bone_3d)
            self.mocap_bridge.start()

        # VRM 3Dプレビュー。WebEngineが利用できる配布環境では実際のVRM
        # モデルを表示し、同じVMC受信経路からHumanoidボーンと表情を駆動する。
        # WebEngineがない開発環境では従来のヘッドプロキシをフォールバック表示する。
        self.preview_3d_dock: QDockWidget | None = None
        if _PREVIEW_3D_AVAILABLE:
            if _VRM_VIEWER_AVAILABLE:
                self.preview_3d_widget = VRMViewerWidget()
            else:
                self.preview_3d_widget = Head3DPreviewWidget()
            self.preview_3d_dock = QDockWidget("3Dキャラクター / VRMプレビュー", self)
            self.preview_3d_dock.setWidget(self.preview_3d_widget)
            self.preview_3d_dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            )
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.preview_3d_dock)
            self.preview_3d_dock.hide()

            if self.mocap_bridge is not None:
                self.mocap_bridge.head_quaternion_received.connect(self.preview_3d_widget.set_head_quaternion)
                if _VRM_VIEWER_AVAILABLE:
                    self.mocap_bridge.blend_shapes_received.connect(self.preview_3d_widget.set_blend_shapes)
                    self.mocap_bridge.bone_transform_3d_received.connect(
                        self.preview_3d_widget.set_bone_transform_3d
                    )

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
        character_action = timeline_menu.addAction("2Dキャラクタークリップを追加...")
        character_action.triggered.connect(self.add_2d_character_clip)
        timeline_menu.addSeparator()
        self._export_audio_mix_action = timeline_menu.addAction("音声ミックスをWAVに書き出す...")
        self._export_audio_mix_action.triggered.connect(self.export_timeline_audio_mix)
        self._export_audio_video_action = timeline_menu.addAction(
            "完成映像にタイムライン音声を統合してMP4に書き出す..."
        )
        self._export_audio_video_action.triggered.connect(self.export_timeline_audio_video)
        timeline_menu.addSeparator()
        self._export_timeline_video_action = timeline_menu.addAction(
            "タイムライン全体をMP4に書き出す..."
        )
        self._export_timeline_video_action.triggered.connect(self.export_timeline_full_video)

        motion_menu = menu_bar.addMenu("モーションキャプチャ")
        self._motion_capture_start_action = motion_menu.addAction("録画開始")
        self._motion_capture_start_action.triggered.connect(self.start_motion_capture_recording)
        self._motion_capture_stop_action = motion_menu.addAction("録画停止・タイムラインに保存")
        self._motion_capture_stop_action.triggered.connect(self.stop_motion_capture_recording)
        self._motion_capture_stop_action.setEnabled(False)
        self._motion_capture_start_action.setEnabled(self.mocap_bridge is not None)

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

    def _on_synth_audio_ready(self, line_id: str, wav_path: str) -> None:
        fingerprint = self.script_panel.synthesis_fingerprint(line_id)
        if fingerprint:
            self._pending_generated_audio[line_id] = (wav_path, fingerprint)

    def _persist_pending_generated_audio(self) -> bool:
        """Copy completed synthesis WAVs into the saved project's managed assets."""
        if self._project.file_path is None or not self._pending_generated_audio:
            return False

        records = self._project.settings.get("generated_audio_assets")
        if not isinstance(records, dict):
            records = {}
        self._project.settings["generated_audio_assets"] = records
        completed: list[str] = []
        changed = False

        for line_id, (source_name, fingerprint) in tuple(self._pending_generated_audio.items()):
            if self.script_panel.synthesis_fingerprint(line_id) != fingerprint:
                completed.append(line_id)
                continue
            source_path = Path(source_name).expanduser()
            if not source_path.is_file():
                self.statusBar().showMessage(
                    f"生成音声ファイルが見つかりません: {source_path}", 8000
                )
                continue

            record = records.get(line_id)
            asset = None
            if isinstance(record, dict) and record.get("fingerprint") == fingerprint:
                asset_id = record.get("asset_id")
                if isinstance(asset_id, str):
                    asset = self._project.get_asset(asset_id)
                    if asset is not None:
                        try:
                            if not self._project.resolve_asset(asset).is_file():
                                asset = None
                        except (OSError, ProjectError, ValueError):
                            asset = None

            created_asset = False
            try:
                if asset is None:
                    asset = self._project.add_asset(
                        source_path, "audio", copy_into_project=True
                    )
                    created_asset = True
                stored_path = self._project.resolve_asset(asset)
                if not self.script_panel.set_persisted_synth_audio(
                    line_id, str(stored_path), fingerprint
                ):
                    if created_asset:
                        self._project.remove_asset(asset.id, delete_managed_file=True)
                    continue
                records[line_id] = {
                    "asset_id": asset.id,
                    "fingerprint": fingerprint,
                }
                changed = True
                completed.append(line_id)
            except (OSError, ProjectError, ValueError) as exc:
                self.statusBar().showMessage(
                    f"生成音声をプロジェクトに保存できませんでした: {exc}", 8000
                )

        timeline_changed = self._sync_generated_voice_timeline()
        changed = changed or timeline_changed
        if changed or completed:
            try:
                self._sync_project_script()
                self._project.save()
            except (OSError, ProjectError) as exc:
                self._project_dirty = True
                self.statusBar().showMessage(
                    f"生成音声のプロジェクト保存に失敗しました: {exc}", 8000
                )
                return False
            for line_id in completed:
                self._pending_generated_audio.pop(line_id, None)
            self._project_dirty = False
            self._refresh_window_title()
            if changed:
                self.statusBar().showMessage(
                    f"生成音声をプロジェクトに保存しました ({len(completed)}行)", 6000
                )
        return changed or bool(completed)

    def _sync_generated_voice_timeline(self) -> bool:
        """Place valid generated narration WAVs on one dedicated Voice track."""
        model = self.timeline_panel.model
        valid_lines = [
            line for line in self.script_panel.model.lines
            if line.line_id in self.script_panel._synth_wav_paths
            and self.script_panel._synth_input_fingerprints.get(line.line_id)
            == self.script_panel.synthesis_fingerprint(line.line_id)
        ]
        valid_ids = {line.line_id for line in valid_lines}
        changed = False

        # Never leave an outdated narration clip playable after the text or
        # speaker assignment changes. The managed source asset remains in the
        # project until the user explicitly removes it.
        old_clip_count = len(model.clips)
        model.clips = [
            clip for clip in model.clips
            if not (
                clip.kind == "voice"
                and clip.script_line_id is not None
                and clip.script_line_id not in valid_ids
            )
        ]
        changed = len(model.clips) != old_clip_count

        if not valid_lines:
            if changed:
                self.timeline_panel.refresh()
                self.timeline_panel.changed.emit()
            return changed

        voice_track = next(
            (
                track for track in model.tracks
                if track.kind == "audio" and track.name.lower() == "voice" and not track.locked
            ),
            None,
        )
        if voice_track is None:
            suffix = 1 + sum(
                1 for track in model.tracks
                if track.kind == "audio" and track.name.lower().startswith("voice")
            )
            voice_track = model.add_track("Voice" if suffix == 1 else f"Voice {suffix}", "audio")
            changed = True

        timeline_start = 0.0
        for line in self.script_panel.model.lines:
            fingerprint = self.script_panel._synth_input_fingerprints.get(line.line_id)
            wav_path = self.script_panel._synth_wav_paths.get(line.line_id)
            record = self._project.settings.get("generated_audio_assets", {})
            record = record.get(line.line_id) if isinstance(record, dict) else None
            asset_id = record.get("asset_id") if isinstance(record, dict) else None
            asset = self._project.get_asset(asset_id) if isinstance(asset_id, str) else None
            if (
                fingerprint
                and self.script_panel.synthesis_fingerprint(line.line_id) == fingerprint
                and isinstance(wav_path, str)
                and Path(wav_path).is_file()
                and asset is not None
                and asset.kind == "audio"
            ):
                clip_name = f"{line.speaker_name}: {line.text[:28]}".strip()
                duration = max(0.05, line.duration_sec)
                _voice_clip, clip_changed = model.upsert_voice_clip(
                    voice_track.id,
                    clip_name or "Voice",
                    duration,
                    start=timeline_start,
                    asset_id=asset.id,
                    script_line_id=line.line_id,
                )
                changed = changed or clip_changed
            timeline_start += line.duration_sec

        if changed:
            self.timeline_panel.refresh()
            self.timeline_panel.changed.emit()
        return changed

    def _restore_generated_audio_assets(self) -> None:
        """Restore cached WAVs only when their source text and voice still match."""
        records = self._project.settings.get("generated_audio_assets", {})
        if not isinstance(records, dict):
            return
        for line_id, record in records.items():
            if not isinstance(line_id, str) or not isinstance(record, dict):
                continue
            asset_id = record.get("asset_id")
            fingerprint = record.get("fingerprint")
            if not isinstance(asset_id, str) or not isinstance(fingerprint, str):
                continue
            asset = self._project.get_asset(asset_id)
            if asset is None or asset.kind != "audio":
                continue
            try:
                path = self._project.resolve_asset(asset)
                if path.is_file():
                    self.script_panel.set_persisted_synth_audio(
                        line_id, str(path), fingerprint
                    )
            except (OSError, ProjectError, ValueError):
                continue
        self._sync_generated_voice_timeline()

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
        self._persist_pending_generated_audio()
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
        self._persist_pending_generated_audio()
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
        if self._motion_capture_recorder.is_recording:
            QMessageBox.information(
                self,
                "モーション録画中",
                "プロジェクトを切り替える前に、モーション録画を停止してください。",
            )
            return False
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
        self._pending_generated_audio.clear()
        self.preview_panel.close_video()
        self.timeline_panel.set_model(TimelineModel.create_default())
        self.timeline_audio.prune_removed_clips()
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
        self._pending_generated_audio.clear()
        self.timeline_panel.set_model(timeline_model)
        self.timeline_audio.prune_removed_clips()
        self._project = project
        self._project_dirty = recovered
        self._restore_generated_audio_assets()
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
                        if self.preview_panel.is_playing and self._timeline_playback_requested:
                            self.timeline_audio.start(active_clip.start)
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

    def _resolve_timeline_audio_asset(self, clip):
        asset = self._project.get_asset(clip.asset_id or "")
        if asset is None:
            raise ProjectError(f"音声クリップ「{clip.name}」の素材参照がありません")
        return self._project.resolve_asset(asset)

    def toggle_timeline_playback(self) -> None:
        """Start/pause the whole timeline, including projects with audio only."""
        if self._timeline_playback_requested:
            self._timeline_playback_requested = False
            self._timeline_playback_generation += 1
            self._pending_timeline_clip_id = None
            self._pause_preview_for_transition()
            self.timeline_audio.pause()
            self.timeline_panel.set_playback_state(False)
            return

        start_seconds = self.timeline_panel.playhead_seconds
        self._timeline_playback_requested = True
        self._timeline_playback_generation += 1
        generation = self._timeline_playback_generation
        self._pending_timeline_clip_id = None
        self.timeline_panel.set_playback_state(True)
        self.timeline_audio.start(start_seconds)

        clip = self._video_clip_at(start_seconds)
        if clip is not None and clip.asset_id is not None:
            self._pending_timeline_clip_id = clip.id
            self._start_timeline_video_clip(
                clip.id, generation, timeline_seconds=start_seconds
            )

    def stop_timeline_playback(self) -> None:
        """Stop video and audio and return the playhead to the beginning."""
        self._timeline_playback_requested = False
        self._timeline_playback_generation += 1
        self._pending_timeline_clip_id = None
        self._pause_preview_for_transition()
        self.timeline_audio.pause()
        self.timeline_panel.set_playback_state(False)
        self.timeline_panel.set_playhead(0.0)
        self._update_vrm_timeline_pose(0.0)
        if self.preview_panel.decoder is not None:
            clip = self._video_clip_at(0.0)
            active_asset_id = self._project.settings.get("active_video_asset_id")
            if clip is not None and clip.asset_id == active_asset_id:
                self._active_timeline_video_clip_id = clip.id
                self.preview_panel.seek_to(clip.source_time_at(0.0))
            else:
                self._active_timeline_video_clip_id = None
                self.preview_panel.seek_to(0.0)

    def _on_audio_timeline_position_changed(self, seconds: float) -> None:
        """Drive audio-only sections and bridge into video clips when encountered."""
        if not self._timeline_playback_requested or self.preview_panel.is_playing:
            return
        self.timeline_panel.set_playhead(seconds)
        self._update_vrm_timeline_pose(seconds)

        if self._pending_timeline_clip_id is None:
            clip = self._video_clip_at(seconds)
            if clip is not None and clip.asset_id is not None:
                self._timeline_playback_generation += 1
                generation = self._timeline_playback_generation
                self._pending_timeline_clip_id = clip.id
                self._start_timeline_video_clip(
                    clip.id, generation, timeline_seconds=seconds
                )
                return

        duration = self.timeline_panel.model.duration_sec
        if self._pending_timeline_clip_id is None and seconds >= duration:
            self.timeline_panel.set_playhead(duration)
            self._timeline_playback_requested = False
            self.timeline_audio.pause()
            self.timeline_panel.set_playback_state(False)

    def _on_video_time_changed(self, source_seconds: float) -> None:
        """Map video PTS to the timeline and synchronize the audio transport."""
        clip = self.timeline_panel.model.get_clip(self._active_timeline_video_clip_id or "")
        if clip is not None and clip.kind == "video":
            timeline_seconds = clip.timeline_time_at(source_seconds)
            self.timeline_panel.set_playhead(timeline_seconds)
            self._update_vrm_timeline_pose(timeline_seconds)
            if self._timeline_playback_requested and self.preview_panel.is_playing:
                if self.timeline_audio.is_playing:
                    self.timeline_audio.sync_to_timeline(timeline_seconds)
                else:
                    self.timeline_audio.start(timeline_seconds)
            if (
                self._timeline_playback_requested
                and self.preview_panel.is_playing
                and source_seconds >= clip.source_out - 1e-6
            ):
                self._advance_timeline_video_clip(clip)
        else:
            self.timeline_panel.set_playhead(source_seconds)
            self._update_vrm_timeline_pose(source_seconds)

    def _on_preview_playback_state_changed(self, playing: bool) -> None:
        """Track user transport changes and start/stop the timeline audio clock."""
        if self._timeline_playback_transition:
            return
        self._timeline_playback_requested = bool(playing)
        self.timeline_panel.set_playback_state(bool(playing))
        if not playing:
            self._timeline_playback_generation += 1
            self._pending_timeline_clip_id = None
            self.timeline_audio.pause()
            return
        if self.timeline_audio.is_playing:
            self.timeline_audio.sync_to_timeline(self.timeline_panel.playhead_seconds)
        else:
            self.timeline_audio.start(self.timeline_panel.playhead_seconds)

    def _on_preview_playback_finished(self) -> None:
        """Continue to the next timeline clip when the current source reaches EOF."""
        if not self._timeline_playback_requested:
            return
        clip = self.timeline_panel.model.get_clip(self._active_timeline_video_clip_id or "")
        if clip is None or clip.kind != "video":
            self._timeline_playback_requested = False
            self.timeline_audio.pause()
            self.timeline_panel.set_playback_state(False)
            return
        self._advance_timeline_video_clip(clip)

    def _pause_preview_for_transition(self) -> None:
        self._timeline_playback_transition = True
        try:
            self.preview_panel.pause_playback()
        finally:
            self._timeline_playback_transition = False

    def _advance_timeline_video_clip(self, clip) -> None:
        """Advance after a video segment, preserving intentional timeline gaps."""
        if not self._timeline_playback_requested or self._pending_timeline_clip_id is not None:
            return
        next_clip = self.timeline_panel.model.next_clip_after(clip.id)
        self.timeline_panel.set_playhead(clip.end)
        if self.timeline_audio.is_playing:
            self.timeline_audio.sync_to_timeline(clip.end)
        self._timeline_playback_generation += 1
        generation = self._timeline_playback_generation

        if next_clip is None:
            # Keep the timeline clock/audio running until the longest audio
            # clip reaches the end of the complete timeline.
            self._pending_timeline_clip_id = None
            self._pause_preview_for_transition()
            return

        self._pending_timeline_clip_id = next_clip.id
        self._pause_preview_for_transition()
        gap_seconds = max(0.0, next_clip.start - clip.end)
        if gap_seconds > 1e-4:
            QTimer.singleShot(
                max(1, int(round(gap_seconds * 1000.0))),
                lambda clip_id=next_clip.id, token=generation: self._start_timeline_video_clip(
                    clip_id, token
                ),
            )
        else:
            self._start_timeline_video_clip(next_clip.id, generation)

    def _abort_timeline_video_transition(self, message: str | None = None) -> None:
        """Stop the complete timeline transport after a failed video transition."""
        self._timeline_playback_requested = False
        self._pending_timeline_clip_id = None
        self.timeline_audio.pause()
        self.timeline_panel.set_playback_state(False)
        if message:
            self.statusBar().showMessage(message, 6000)

    def _start_timeline_video_clip(
        self, clip_id: str, generation: int, *, timeline_seconds: float | None = None
    ) -> None:
        """Open/seek a timeline clip and resume if the transition is current."""
        if (
            generation != self._timeline_playback_generation
            or not self._timeline_playback_requested
            or self._pending_timeline_clip_id not in (None, clip_id)
        ):
            return
        clip = self.timeline_panel.model.get_clip(clip_id)
        if clip is None or clip.kind != "video" or clip.asset_id is None:
            self._abort_timeline_video_transition()
            return
        asset = self._project.get_asset(clip.asset_id)
        if asset is None:
            self._abort_timeline_video_transition("次の動画クリップの素材が見つかりません")
            return
        try:
            path = self._project.resolve_asset(asset)
            if not path.is_file():
                self._abort_timeline_video_transition(f"動画素材が見つかりません: {path}")
                return
            previous_asset_id = self._project.settings.get("active_video_asset_id")
            changed = (
                self._project.settings.get("active_timeline_video_clip_id") != clip.id
                or previous_asset_id != asset.id
            )
            self._active_timeline_video_clip_id = clip.id
            self._pending_timeline_clip_id = None
            self._project.settings["active_timeline_video_clip_id"] = clip.id
            self._project.settings["active_video_asset_id"] = asset.id
            target_timeline_seconds = clip.start if timeline_seconds is None else min(
                max(clip.start, timeline_seconds), clip.end - 1e-6
            )
            source_seconds = clip.source_time_at(target_timeline_seconds)
            self.timeline_panel.set_playhead(target_timeline_seconds)
            if changed:
                self._mark_project_dirty()

            self._timeline_playback_transition = True
            try:
                if self.preview_panel.decoder is None or previous_asset_id != asset.id:
                    opened = self.preview_panel.open_video(str(path))
                    # open_video() starts the source preview at frame zero.
                    # Timeline clips may be trimmed, so seek to source_in before
                    # the next timer tick decodes the first playback frame.
                    if opened:
                        opened = self.preview_panel.seek_to(source_seconds)
                else:
                    opened = self.preview_panel.seek_to(source_seconds)
                    if opened:
                        self.preview_panel.resume_playback()
                if not opened:
                    # open_video() starts the decoder timer before the trim-in
                    # seek. If that seek fails, close the just-opened decoder so
                    # no untracked source playback can continue in the background.
                    self.preview_panel.close_video()
            finally:
                self._timeline_playback_transition = False
            if not opened:
                self._abort_timeline_video_transition(f"動画を開けませんでした: {path}")
                return
            self._timeline_playback_requested = True
            if self.timeline_audio.is_playing:
                self.timeline_audio.sync_to_timeline(target_timeline_seconds)
            else:
                self.timeline_audio.start(target_timeline_seconds)
            self.timeline_panel.set_playback_state(True)
        except (OSError, ProjectError) as exc:
            self._abort_timeline_video_transition(f"次の動画クリップを開けません: {exc}")

    def _on_timeline_playhead_changed(self, seconds: float) -> None:
        """Seek source media using the clip mapping at the requested timeline time."""
        if not self._timeline_playback_transition:
            self._timeline_playback_generation += 1
            self._pending_timeline_clip_id = None
        self._update_vrm_timeline_pose(seconds)
        if self.timeline_audio.is_playing:
            self.timeline_audio.sync_to_timeline(seconds)
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
        if not self._timeline_playback_transition:
            self._timeline_playback_generation += 1
            self._pending_timeline_clip_id = None
        if not clip_id:
            return
        clip = self.timeline_panel.model.get_clip(clip_id)
        if clip is None or clip.asset_id is None:
            return
        if clip.kind == "character" and _VRM_VIEWER_AVAILABLE:
            character_asset = self._project.get_asset(clip.asset_id)
            if character_asset is None:
                self.statusBar().showMessage("キャラクター素材が見つかりません", 5000)
                return
            try:
                character_path = self._project.resolve_asset(character_asset)
                if character_path.suffix.lower() != ".vrm":
                    self.statusBar().showMessage(
                        "現在の3DプレビューはVRM 0.x / 1.0の .vrm 素材に対応しています",
                        6000,
                    )
                    return
                self.preview_3d_widget.load_model(str(character_path))
                if self.preview_3d_dock is not None:
                    self.preview_3d_dock.show()
            except (OSError, ProjectError) as exc:
                QMessageBox.warning(self, "VRMモデルを開けません", str(exc))
            return
        if clip.kind != "video":
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
            if self._timeline_playback_requested:
                if self.timeline_audio.is_playing:
                    self.timeline_audio.sync_to_timeline(clip.start)
                else:
                    self.timeline_audio.start(clip.start)
        except (OSError, ProjectError) as exc:
            QMessageBox.warning(self, "動画クリップを開けません", str(exc))

    def _on_timeline_edited(self) -> None:
        self._mark_project_dirty()
        self.timeline_audio.prune_removed_clips()
        clip = self.timeline_panel.model.get_clip(self._active_timeline_video_clip_id or "")
        if clip is None:
            self._active_timeline_video_clip_id = None
            self._project.settings.pop("active_timeline_video_clip_id", None)
            return
        if clip.kind == "video" and self.preview_panel.decoder is not None:
            self.preview_panel.seek_to(clip.source_time_at(self.timeline_panel.playhead_seconds))

    def export_timeline_audio_mix(self) -> None:
        """Export all enabled, unmuted timeline audio/voice clips as one WAV mix."""
        if (
            self._audio_render_task is not None
            or self._audio_video_export_task is not None
            or self._timeline_video_export_task is not None
        ):
            self.statusBar().showMessage("音声・動画を書き出し中です", 5000)
            return

        try:
            snapshot = TimelineModel.from_dict(self.timeline_panel.model.to_dict())
        except TimelineError as exc:
            QMessageBox.warning(self, "音声ミックス", f"タイムラインを読み込めません: {exc}")
            return

        clips = timeline_audio_clips(snapshot)
        if not clips:
            QMessageBox.information(
                self, "音声ミックス", "有効でミュートされていない音声クリップがありません。"
            )
            return

        if self._project.file_path is not None:
            suggested = self._project.file_path.with_name(
                f"{self._project.file_path.stem}_audio_mix.wav"
            )
        else:
            suggested = Path.home() / "aural_audio_mix.wav"
        output_name, _ = QFileDialog.getSaveFileName(
            self,
            "タイムライン音声ミックスを書き出す",
            str(suggested),
            "WAV音声 (*.wav)",
        )
        if not output_name:
            return

        output_path = Path(output_name).expanduser()
        if output_path.suffix.lower() != ".wav":
            QMessageBox.warning(self, "音声ミックス", "出力先は .wav ファイルを指定してください。")
            return

        try:
            asset_paths = {
                clip.id: str(self._resolve_timeline_audio_asset(clip).expanduser().resolve())
                for clip in clips
            }
        except (OSError, ProjectError, ValueError) as exc:
            QMessageBox.warning(self, "音声素材エラー", str(exc))
            return

        resolved_output = output_path.resolve()
        if resolved_output in {Path(path).resolve() for path in asset_paths.values()}:
            QMessageBox.warning(
                self,
                "音声ミックス",
                "元の音声素材を上書きすることはできません。別の出力先を指定してください。",
            )
            return

        task = _TimelineAudioRenderTask(
            snapshot.to_dict(),
            asset_paths,
            str(output_path),
        )
        task.signals.finished.connect(self._on_timeline_audio_render_finished)
        task.signals.failed.connect(self._on_timeline_audio_render_failed)
        self._audio_render_task = task
        self._set_audio_export_actions_enabled(False)
        self.statusBar().showMessage(
            f"音声ミックスを書き出しています... ({len(clips)} クリップ)", 0
        )
        self._audio_render_pool.start(task)

    def _set_audio_export_actions_enabled(self, enabled: bool) -> None:
        if self._export_audio_mix_action is not None:
            self._export_audio_mix_action.setEnabled(enabled)
        if self._export_audio_video_action is not None:
            self._export_audio_video_action.setEnabled(enabled)
        if self._export_timeline_video_action is not None:
            self._export_timeline_video_action.setEnabled(enabled)

    def _finish_timeline_audio_render(self) -> None:
        self._audio_render_task = None
        self._set_audio_export_actions_enabled(
            self._audio_video_export_task is None
            and self._timeline_video_export_task is None
        )

    def _on_timeline_audio_render_finished(self, result) -> None:
        self._finish_timeline_audio_render()
        summary = (
            f"音声ミックスを書き出しました。\\n"
            f"保存先: {result.output_path}\\n"
            f"長さ: {result.duration_seconds:.2f} 秒 / "
            f"サンプルレート: {result.sample_rate} Hz / "
            f"クリップ数: {result.audio_clip_count}"
        )
        if result.clipped_sample_count:
            summary += (
                f"\\n\\n注意: {result.clipped_sample_count:,} サンプルが上限を超えたため、"
                "出力時にクリップされました。クリップ音量を下げて再書き出ししてください。"
            )
            QMessageBox.warning(self, "音声ミックス完了（音割れの可能性）", summary)
        else:
            QMessageBox.information(self, "音声ミックス完了", summary)
        self.statusBar().showMessage(f"WAVを書き出しました: {result.output_path}", 8000)

    def _on_timeline_audio_render_failed(self, message: str) -> None:
        self._finish_timeline_audio_render()
        QMessageBox.critical(self, "音声ミックスに失敗しました", message)
        self.statusBar().showMessage("音声ミックスの書き出しに失敗しました", 8000)


    def export_timeline_audio_video(self) -> None:
        """Render the timeline mix and add it to an existing composed video."""
        if (
            self._audio_render_task is not None
            or self._audio_video_export_task is not None
            or self._timeline_video_export_task is not None
        ):
            self.statusBar().showMessage("別のメディア書き出しが実行中です", 5000)
            return

        try:
            ffmpeg_path = find_ffmpeg_executable()
        except MediaToolError as exc:
            QMessageBox.warning(self, "FFmpegが必要です", str(exc))
            return

        try:
            snapshot = TimelineModel.from_dict(self.timeline_panel.model.to_dict())
        except TimelineError as exc:
            QMessageBox.warning(self, "映像・音声の統合", f"タイムラインを読み込めません: {exc}")
            return
        clips = timeline_audio_clips(snapshot)
        if not clips:
            QMessageBox.information(
                self, "映像・音声の統合", "有効でミュートされていない音声クリップがありません。"
            )
            return

        video_name, _ = QFileDialog.getOpenFileName(
            self,
            "音声を統合する完成映像を選択",
            str(self._project.file_path.parent if self._project.file_path else Path.home()),
            "動画ファイル (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;すべてのファイル (*)",
        )
        if not video_name:
            return
        video_path = Path(video_name).expanduser().resolve()

        suggested = video_path.with_name(f"{video_path.stem}_with_audio.mp4")
        output_name, _ = QFileDialog.getSaveFileName(
            self,
            "音声付きMP4を書き出す",
            str(suggested),
            "MP4動画 (*.mp4)",
        )
        if not output_name:
            return
        output_path = Path(output_name).expanduser()
        if output_path.suffix.lower() != ".mp4":
            QMessageBox.warning(self, "映像・音声の統合", "出力先は .mp4 を指定してください。")
            return

        try:
            asset_paths = {
                clip.id: str(self._resolve_timeline_audio_asset(clip).expanduser().resolve())
                for clip in clips
            }
        except (OSError, ProjectError, ValueError) as exc:
            QMessageBox.warning(self, "音声素材エラー", str(exc))
            return

        protected_sources = {video_path, *(Path(path).resolve() for path in asset_paths.values())}
        if output_path.resolve() in protected_sources:
            QMessageBox.warning(
                self,
                "映像・音声の統合",
                "元の映像・音声素材を上書きできません。別の出力先を指定してください。",
            )
            return

        task = _TimelineAudioVideoExportTask(
            snapshot.to_dict(),
            asset_paths,
            str(video_path),
            str(output_path),
            ffmpeg_path,
        )
        task.signals.finished.connect(self._on_timeline_audio_video_export_finished)
        task.signals.failed.connect(self._on_timeline_audio_video_export_failed)
        self._audio_video_export_task = task
        self._set_audio_export_actions_enabled(False)
        self.statusBar().showMessage(
            f"タイムライン音声を映像に統合しています... ({len(clips)} クリップ)", 0
        )
        self._audio_render_pool.start(task)

    def _finish_timeline_audio_video_export(self) -> None:
        self._audio_video_export_task = None
        self._set_audio_export_actions_enabled(
            self._audio_render_task is None
            and self._timeline_video_export_task is None
        )

    def _on_timeline_audio_video_export_finished(self, result: dict) -> None:
        self._finish_timeline_audio_video_export()
        summary = (
            "音声付きMP4を書き出しました。\\n"
            f"保存先: {result['output_path']}\\n"
            f"音声クリップ数: {result['audio_clip_count']} / "
            f"ミックス長: {result['duration_seconds']:.2f} 秒\\n"
            "映像ストリームは再エンコードせずコピーし、音声をAACで統合しています。"
        )
        if result["clipped_sample_count"]:
            summary += (
                f"\\n\\n注意: ミックス中に {result['clipped_sample_count']:,} サンプルが"
                "上限を超えました。音量を下げて再出力してください。"
            )
            QMessageBox.warning(self, "MP4書き出し完了（音割れの可能性）", summary)
        else:
            QMessageBox.information(self, "MP4書き出し完了", summary)
        self.statusBar().showMessage(f"MP4を書き出しました: {result['output_path']}", 8000)

    def _on_timeline_audio_video_export_failed(self, message: str) -> None:
        self._finish_timeline_audio_video_export()
        QMessageBox.critical(self, "映像・音声の統合に失敗しました", message)
        self.statusBar().showMessage("MP4への音声統合に失敗しました", 8000)

    def export_timeline_full_video(self) -> None:
        """Render video/image clips from the timeline and include audio when present."""
        if (
            self._audio_render_task is not None
            or self._audio_video_export_task is not None
            or self._timeline_video_export_task is not None
        ):
            self.statusBar().showMessage("別のメディア書き出しが実行中です", 5000)
            return

        try:
            ffmpeg_path = find_ffmpeg_executable()
        except MediaToolError as exc:
            QMessageBox.warning(self, "FFmpegが必要です", str(exc))
            return

        try:
            snapshot = TimelineModel.from_dict(self.timeline_panel.model.to_dict())
            subtitle_texts = {
                str(line.get("line_id", "")): str(line.get("text", ""))
                for line in self.script_panel.model.to_dict().get("lines", [])
                if line.get("line_id") and str(line.get("text", "")).strip()
            }
            visual_clips = timeline_video_clips(snapshot, subtitle_texts)
        except (TimelineError, VideoRenderError) as exc:
            QMessageBox.warning(self, "タイムラインMP4書き出し", str(exc))
            return
        if not visual_clips:
            QMessageBox.information(
                self,
                "タイムラインMP4書き出し",
                "表示中で有効な動画・静止画・字幕・キャラクタークリップがありません。",
            )
            return

        motion_clips = timeline_motion_clips(snapshot)
        audio_clips = timeline_audio_clips(snapshot)
        all_clips = visual_clips + audio_clips + motion_clips
        try:
            asset_paths: dict[str, str] = {}
            for clip in all_clips:
                if clip.kind in {"text", "character"}:
                    continue
                asset = self._project.get_asset(clip.asset_id or "")
                if asset is None:
                    raise ProjectError(f"クリップ「{clip.name}」の素材参照がありません")
                resolved = self._project.resolve_asset(asset).expanduser().resolve()
                if not resolved.is_file():
                    raise ProjectError(f"素材ファイルが見つかりません: {resolved}")
                asset_paths[clip.id] = str(resolved)
        except (OSError, ProjectError, ValueError) as exc:
            QMessageBox.warning(self, "タイムライン素材エラー", str(exc))
            return

        suggested = (
            self._project.file_path.with_name(
                f"{self._project.file_path.stem}_timeline.mp4"
            )
            if self._project.file_path is not None
            else Path.home() / "aural_timeline.mp4"
        )
        output_name, _ = QFileDialog.getSaveFileName(
            self,
            "タイムライン全体をMP4に書き出す",
            str(suggested),
            "MP4動画 (*.mp4)",
        )
        if not output_name:
            return
        output_path = Path(output_name).expanduser()
        if output_path.suffix.lower() != ".mp4":
            QMessageBox.warning(
                self, "タイムラインMP4書き出し", "出力先は .mp4 を指定してください。"
            )
            return

        protected_sources = {Path(path).resolve() for path in asset_paths.values()}
        if output_path.resolve() in protected_sources:
            QMessageBox.warning(
                self,
                "タイムラインMP4書き出し",
                "元の映像・画像・音声素材を上書きできません。別の出力先を指定してください。",
            )
            return

        task = _TimelineFullVideoExportTask(
            snapshot.to_dict(),
            asset_paths,
            str(output_path),
            ffmpeg_path,
            subtitle_texts,
        )
        task.signals.finished.connect(self._on_timeline_full_video_export_finished)
        task.signals.failed.connect(self._on_timeline_full_video_export_failed)
        self._timeline_video_export_task = task
        self._set_audio_export_actions_enabled(False)
        self.statusBar().showMessage(
            f"タイムライン映像を書き出しています... "
            f"(映像・静止画 {len(visual_clips)} / 音声 {len(audio_clips)} クリップ)",
            0,
        )
        self._audio_render_pool.start(task)

    def _finish_timeline_full_video_export(self) -> None:
        self._timeline_video_export_task = None
        self._set_audio_export_actions_enabled(
            self._audio_render_task is None
            and self._audio_video_export_task is None
        )

    def _on_timeline_full_video_export_finished(self, result: dict) -> None:
        self._finish_timeline_full_video_export()
        summary = (
            "タイムライン全体をMP4に書き出しました。\\n"
            f"保存先: {result['output_path']}\\n"
            f"映像・静止画: {result['video_clip_count']} クリップ / "
            f"字幕: {result['subtitle_clip_count']} クリップ / "
            f"2Dキャラクター: {result['character_clip_count']} クリップ / "
            f"モーション: {result['motion_clip_count']} クリップ / "
            f"音声: {result['audio_clip_count']} クリップ\\n"
            f"解像度: {result['width']}×{result['height']} / "
            f"{result['frame_rate']:g} fps / {result['duration_seconds']:.2f} 秒"
        )
        if result["clipped_sample_count"]:
            summary += (
                f"\\n\\n注意: 音声ミックスで {result['clipped_sample_count']:,} "
                "サンプルが上限を超えました。音量を下げて再出力してください。"
            )
            QMessageBox.warning(self, "MP4書き出し完了（音割れの可能性）", summary)
        else:
            QMessageBox.information(self, "MP4書き出し完了", summary)
        self.statusBar().showMessage(
            f"MP4を書き出しました: {result['output_path']}", 8000
        )

    def _on_timeline_full_video_export_failed(self, message: str) -> None:
        self._finish_timeline_full_video_export()
        QMessageBox.critical(self, "タイムラインMP4書き出しに失敗しました", message)
        self.statusBar().showMessage("タイムラインMP4書き出しに失敗しました", 8000)

    def _record_motion_head(self, x: float, y: float, tilt_deg: float) -> None:
        if self._motion_capture_recorder.is_recording:
            try:
                self._motion_capture_recorder.update_head(x, y, tilt_deg)
            except MotionCaptureRecordingError as exc:
                self.statusBar().showMessage(f"モーション値を無視しました: {exc}", 5000)

    def _record_motion_head_3d(
        self,
        x: float,
        y: float,
        z: float,
        qx: float,
        qy: float,
        qz: float,
        qw: float,
    ) -> None:
        if self._motion_capture_recorder.is_recording:
            try:
                self._motion_capture_recorder.update_head_3d(x, y, z, qx, qy, qz, qw)
            except (MotionCaptureRecordingError, TypeError, ValueError) as exc:
                self.statusBar().showMessage(f"VRM頭部データを無視しました: {exc}", 5000)

    def _record_motion_bone_3d(
        self,
        name: str,
        x: float,
        y: float,
        z: float,
        qx: float,
        qy: float,
        qz: float,
        qw: float,
    ) -> None:
        if self._motion_capture_recorder.is_recording:
            try:
                self._motion_capture_recorder.update_bone_3d(
                    name, x, y, z, qx, qy, qz, qw
                )
            except (MotionCaptureRecordingError, TypeError, ValueError) as exc:
                self.statusBar().showMessage(f"VRMボーンデータを無視しました: {exc}", 5000)

    def _record_motion_blend_shapes(self, values: dict) -> None:
        if self._motion_capture_recorder.is_recording:
            try:
                self._motion_capture_recorder.update_blend_shapes(values)
            except (MotionCaptureRecordingError, TypeError, ValueError) as exc:
                self.statusBar().showMessage(f"表情データを無視しました: {exc}", 5000)

    def _record_motion_bone(
        self, name: str, qx: float, qy: float, qz: float, qw: float
    ) -> None:
        if self._motion_capture_recorder.is_recording:
            try:
                self._motion_capture_recorder.update_bone(name, qx, qy, qz, qw)
            except MotionCaptureRecordingError as exc:
                self.statusBar().showMessage(f"ボーンデータを無視しました: {exc}", 5000)

    def _update_vrm_timeline_pose(self, timeline_seconds: float) -> None:
        """Sample an active motion clip and apply it to the currently active VRM."""
        if not _VRM_VIEWER_AVAILABLE or self.preview_3d_dock is None:
            return
        try:
            model = self.timeline_panel.model
            active_characters = []
            for clip in model.clips:
                if clip.kind != "character" or not clip.enabled:
                    continue
                if not clip.start <= timeline_seconds < clip.end:
                    continue
                track = model.get_track(clip.track_id)
                if track is None or track.muted or not track.visible or clip.asset_id is None:
                    continue
                asset = self._project.get_asset(clip.asset_id)
                if asset is None:
                    continue
                path = self._project.resolve_asset(asset)
                if path.suffix.lower() == ".vrm" and path.is_file():
                    active_characters.append((clip, path))

            if len(active_characters) > 1:
                self.preview_3d_widget.set_timeline_state(None)
                self.statusBar().showMessage(
                    "同じ時間帯に複数のVRMキャラクターがあります。1体ずつ配置してください。",
                    5000,
                )
                return
            if active_characters:
                _clip, character_path = active_characters[0]
                if self.preview_3d_widget.model_path != character_path.resolve():
                    self.preview_3d_widget.load_model(character_path)
                if not self.preview_3d_dock.isVisible():
                    self.preview_3d_dock.show()

            active_motion = []
            for clip in model.clips:
                if clip.kind != "motion" or not clip.enabled or clip.asset_id is None:
                    continue
                if not clip.start <= timeline_seconds < clip.end:
                    continue
                track = model.get_track(clip.track_id)
                if track is None or track.muted or not track.visible:
                    continue
                active_motion.append(clip)

            if not active_motion:
                self.preview_3d_widget.set_timeline_state(None)
                return
            if len(active_motion) > 1:
                self.preview_3d_widget.set_timeline_state(None)
                self.statusBar().showMessage(
                    "VRM再生中のモーションクリップが重なっています。",
                    5000,
                )
                return

            motion_clip = active_motion[0]
            asset = self._project.get_asset(motion_clip.asset_id)
            if asset is None:
                self.preview_3d_widget.set_timeline_state(None)
                return
            source = self._project.resolve_asset(asset)
            stat = source.stat()
            cached = self._vrm_motion_cache.get(asset.id)
            if cached is None or cached[0] != str(source) or cached[1] != stat.st_mtime_ns:
                frames = load_motion_file(source)
                self._vrm_motion_cache[asset.id] = (
                    str(source), stat.st_mtime_ns, frames
                )
            else:
                frames = cached[2]

            local_time = motion_clip.source_in + timeline_seconds - motion_clip.start
            sampled = sample_motion(frames, local_time)
            self.preview_3d_widget.set_timeline_state(
                motion_state_to_vrm_pose(sampled)
            )
        except (OSError, ProjectError, CharacterMotionError, TypeError, ValueError) as exc:
            self.preview_3d_widget.set_timeline_state(None)
            self.statusBar().showMessage(f"VRMモーションを再生できません: {exc}", 6000)

    def start_motion_capture_recording(self) -> None:
        """Start recording live OSC/VMC or WebSocket state into motion keyframes."""
        if self.mocap_bridge is None:
            QMessageBox.information(
                self,
                "モーションキャプチャ録画",
                "OSC/WebSocketのモーションキャプチャ受信が利用できません。",
            )
            return
        if self._motion_capture_recorder.is_recording:
            return
        if self._project.file_path is None and not self.save_project_as():
            return
        try:
            self._motion_capture_recorder.start()
        except MotionCaptureRecordingError as exc:
            QMessageBox.warning(self, "録画を開始できません", str(exc))
            return
        self._motion_capture_timer.start()
        if self._motion_capture_start_action is not None:
            self._motion_capture_start_action.setEnabled(False)
        if self._motion_capture_stop_action is not None:
            self._motion_capture_stop_action.setEnabled(True)
        self.statusBar().showMessage(
            f"モーション録画中 ({MOTION_CAPTURE_FRAME_RATE:g} fps)。"
            "「モーションキャプチャ → 録画停止・タイムラインに保存」で終了します。",
            0,
        )

    def _capture_motion_frame(self) -> None:
        if not self._motion_capture_recorder.is_recording:
            self._motion_capture_timer.stop()
            return
        try:
            self._motion_capture_recorder.capture()
            self.statusBar().showMessage(
                f"モーション録画中 · {self._motion_capture_recorder.keyframe_count} "
                f"キーフレーム · 上限 {MAX_RECORDING_SECONDS:g} 秒",
                0,
            )
        except MotionCaptureRecordingError as exc:
            self.stop_motion_capture_recording()
            QMessageBox.warning(
                self,
                "録画上限に達しました",
                f"{exc}。取得済みのモーションをタイムラインへ保存しました。",
            )

    def stop_motion_capture_recording(self) -> bool:
        """Stop capture, save a managed motion asset, and place it on the timeline."""
        if not self._motion_capture_recorder.is_recording:
            return True
        self._motion_capture_timer.stop()
        if self._motion_capture_start_action is not None:
            self._motion_capture_start_action.setEnabled(self.mocap_bridge is not None)
        if self._motion_capture_stop_action is not None:
            self._motion_capture_stop_action.setEnabled(False)

        try:
            keyframes = self._motion_capture_recorder.stop()
        except MotionCaptureRecordingError as exc:
            QMessageBox.warning(self, "録画を停止できません", str(exc))
            return False

        temporary_path: Path | None = None
        asset = None
        character_asset = None
        character_asset_created = False
        motion_clip_id: str | None = None
        character_clip_id: str | None = None
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            with tempfile.NamedTemporaryFile(
                prefix=f"Motion_Take_{stamp}_",
                suffix=".motion.json",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
            save_motion_file(temporary_path, keyframes)
            asset = self._project.add_asset(
                temporary_path,
                "motion",
                copy_into_project=True,
            )

            model = self.timeline_panel.model
            start = self.timeline_panel.playhead_seconds
            duration = max(0.05, float(keyframes[-1]["time"]))
            end = start + duration
            overlapping_characters = [
                clip for clip in model.clips
                if clip.kind == "character" and clip.enabled
                and clip.start < end and clip.end > start
                and (track := model.get_track(clip.track_id)) is not None
                and not track.muted and track.visible
            ]
            overlapping_vrm_characters = []
            for clip in overlapping_characters:
                if clip.asset_id is None:
                    continue
                existing_character_asset = self._project.get_asset(clip.asset_id)
                if existing_character_asset is None:
                    continue
                try:
                    existing_character_path = self._project.resolve_asset(
                        existing_character_asset
                    )
                except (OSError, ProjectError):
                    continue
                if existing_character_path.suffix.lower() == ".vrm":
                    overlapping_vrm_characters.append(clip)

            loaded_vrm_path = (
                self.preview_3d_widget.model_path
                if _VRM_VIEWER_AVAILABLE and self.preview_3d_dock is not None
                else None
            )
            active_vrm_model = (
                loaded_vrm_path is not None and loaded_vrm_path.is_file()
            )
            # A pre-existing 2D character clip must not suppress the VRM clip:
            # otherwise a saved VRM take has no model reference when reopened.
            should_add_character = should_add_character_clip_for_recording(
                has_any_character_clip=bool(overlapping_characters),
                has_vrm_character_clip=bool(overlapping_vrm_characters),
                has_loaded_vrm_model=active_vrm_model,
            )
            needs_vrm_character = should_add_character and active_vrm_model
            needs_2d_character = should_add_character and not active_vrm_model

            if needs_vrm_character or needs_2d_character:
                overlay_tracks = [
                    track for track in model.tracks
                    if track.kind == "overlay"
                    and not track.locked and not track.muted and track.visible
                ]
                if not overlay_tracks:
                    count = 1 + sum(
                        1 for track in model.tracks if track.kind == "overlay"
                    )
                    overlay_tracks = [model.add_track(f"Overlay {count}", "overlay")]

                if active_vrm_model:
                    existing_asset_ids = {item.id for item in self._project.assets}
                    character_asset = self._project.add_asset(
                        loaded_vrm_path,
                        "character",
                        copy_into_project=False,
                    )
                    character_asset_created = character_asset.id not in existing_asset_ids
                    character = model.add_clip(
                        overlay_tracks[0].id,
                        "character",
                        character_asset.name,
                        duration,
                        start=start,
                        asset_id=character_asset.id,
                    )
                else:
                    character = model.add_clip(
                        overlay_tracks[0].id,
                        "character",
                        "2D Character",
                        duration,
                        start=start,
                        script_line_id=BUILTIN_2D_CHARACTER_ID,
                    )
                character_clip_id = character.id

            motion_tracks = [
                track for track in model.tracks
                if track.kind == "motion"
                and not track.locked and not track.muted and track.visible
            ]
            if not motion_tracks:
                count = 1 + sum(
                    1 for track in model.tracks if track.kind == "motion"
                )
                motion_tracks = [model.add_track(f"Motion {count}", "motion")]
            motion = model.add_clip(
                motion_tracks[0].id,
                "motion",
                f"Motion Take {stamp}",
                duration,
                start=start,
                asset_id=asset.id,
            )
            motion_clip_id = motion.id
            self.timeline_panel.refresh()
            self._mark_project_dirty()

            if not self.save_project():
                self.statusBar().showMessage(
                    "モーションはプロジェクトに追加されましたが、プロジェクトファイルは未保存です。",
                    8000,
                )
                return False
            self.statusBar().showMessage(
                f"モーションを保存してタイムラインに配置しました: {asset.name}",
                8000,
            )
            return True
        except (OSError, ProjectError, TimelineError, MotionCaptureRecordingError, ValueError) as exc:
            if motion_clip_id is not None:
                self.timeline_panel.model.remove_clip(motion_clip_id)
            if character_clip_id is not None:
                self.timeline_panel.model.remove_clip(character_clip_id)
            if asset is not None:
                try:
                    self._project.remove_asset(asset.id, delete_managed_file=True)
                except (OSError, ProjectError):
                    pass
            if character_asset is not None and character_asset_created:
                try:
                    self._project.remove_asset(
                        character_asset.id,
                        delete_managed_file=False,
                    )
                except (OSError, ProjectError):
                    pass
            self.timeline_panel.refresh()
            self._mark_project_dirty()
            QMessageBox.critical(
                self,
                "モーション保存エラー",
                f"録画データをプロジェクトへ保存できませんでした: {exc}",
            )
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def add_2d_character_clip(self) -> None:
        """Add the built-in 2D placeholder character to the overlay track."""
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
        duration, accepted = QInputDialog.getDouble(
            self,
            "2Dキャラクターを追加",
            "表示時間 (秒):",
            5.0,
            0.05,
            3600.0,
            2,
        )
        if not accepted:
            return
        try:
            self.timeline_panel.model.add_clip(
                tracks[0].id,
                "character",
                "2D Character",
                duration,
                start=self.timeline_panel.playhead_seconds,
                script_line_id=BUILTIN_2D_CHARACTER_ID,
            )
        except TimelineError as exc:
            QMessageBox.warning(self, "キャラクター追加エラー", str(exc))
            return
        self.timeline_panel.refresh()
        self._mark_project_dirty()
        self.statusBar().showMessage(
            "2Dプレースホルダーキャラクターをタイムラインに追加しました",
            5000,
        )

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
        if asset.kind == "motion":
            try:
                from character_motion import load_motion_file, motion_duration
                duration = motion_duration(load_motion_file(self._project.resolve_asset(asset)))
                if duration >= 0.05:
                    return duration
            except (OSError, ValueError, ProjectError):
                pass
            return 5.0
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
            if asset.kind in {"video", "audio", "image", "character", "motion", "other"}
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
            "motion": ("motion", "motion"),
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
        existing = sorted(
            (
                clip for clip in self.timeline_panel.model.clips
                if clip.kind == "video" and clip.asset_id == asset.id
            ),
            key=lambda clip: (clip.start, clip.id),
        )
        if existing:
            clip = existing[0]
            self.timeline_panel.canvas._set_selected_clip(clip.id)
        else:
            tracks = [
                track for track in self.timeline_panel.model.tracks
                if track.kind == "video" and not track.locked
            ]
            if not tracks:
                tracks = [self.timeline_panel.model.add_track("Video 1", "video")]
            duration = self._asset_default_duration(asset)
            try:
                clip = self.timeline_panel.add_clip(
                    tracks[0].id,
                    "video",
                    asset.name,
                    duration,
                    start=0.0,
                    asset_id=asset.id,
                )
            except TimelineError as exc:
                QMessageBox.warning(self, "タイムライン追加エラー", str(exc))
                return

        # A video opened from the File menu/startup is also an active timeline
        # clip. Record the mapping explicitly so playback stops/advances at its
        # clip boundary instead of treating source PTS as timeline time.
        self._active_timeline_video_clip_id = clip.id
        self._project.settings["active_video_asset_id"] = asset.id
        self._project.settings["active_timeline_video_clip_id"] = clip.id
        self.timeline_panel.set_playhead(clip.start)
        self.preview_panel.seek_to(clip.source_in)
        if self.preview_panel.is_playing:
            self.timeline_audio.start(clip.start)
        self._mark_project_dirty()

    def _guess_asset_kind(self, path: str) -> str:
        extension = Path(path).suffix.lower()
        if extension in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
            return "video"
        if extension in {".wav", ".mp3", ".flac", ".aac", ".m4a", ".ogg"}:
            return "audio"
        if extension in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            return "image"
        if Path(path).name.lower().endswith(".motion.json"):
            return "motion"
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
            "メディア/素材 (*.mp4 *.mov *.mkv *.avi *.webm *.wav *.mp3 *.flac *.aac *.m4a *.ogg *.png *.jpg *.jpeg *.webp *.bmp *.vrm *.vmd *.fbx *.obj *.glb *.gltf *.motion.json);;すべてのファイル (*)",
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
        if (
            self._audio_render_task is not None
            or self._audio_video_export_task is not None
            or self._timeline_video_export_task is not None
        ):
            QMessageBox.information(
                self,
                "メディアを書き出し中",
                "書き出しが完了するまでアプリを閉じることはできません。",
            )
            event.ignore()
            return
        if self._motion_capture_recorder.is_recording:
            if not self.stop_motion_capture_recording():
                event.ignore()
                return
        if not self._confirm_discard_changes():
            event.ignore()
            return
        if self._discard_pending:
            self._remove_autosave_file(self._project.autosave_path)
            self._discard_pending = False
        self._autosave_timer.stop()
        self.timeline_audio.stop()
        self.preview_panel.cleanup()
        self.script_panel.cleanup()
        if self.mocap_bridge is not None:
            self.mocap_bridge.stop()
        if _VRM_VIEWER_AVAILABLE and self.preview_3d_dock is not None:
            self.preview_3d_widget.shutdown()
        super().closeEvent(event)


def main() -> int:
    if "--runtime-check" in sys.argv[1:]:
        library_path = _default_vose_lib_path()
        if library_path is None:
            print("VO-SE native library was not found in the application bundle", file=sys.stderr)
            return 2
        try:
            if not _VRM_VIEWER_AVAILABLE:
                raise RuntimeError(
                    "Qt WebEngine VRM preview is unavailable in this application bundle"
                )
            from vose_engine import VoseEngine

            version = VoseEngine(library_path).get_engine_version()
            ffmpeg = find_ffmpeg_executable()
            ffprobe = find_ffprobe_executable(ffmpeg_path=ffmpeg)
            for tool_path in (ffmpeg, ffprobe):
                result = subprocess.run(
                    [tool_path, "-version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    raise RuntimeError(
                        f"{Path(tool_path).name} could not start successfully: {detail}"
                    )
        except Exception as exc:
            print(f"Runtime validation failed: {exc}", file=sys.stderr)
            return 3
        print(f"Axral Cut Studio runtime OK; VO-SE engine {version:g}")
        print(f"FFmpeg: {ffmpeg}")
        print(f"FFprobe: {ffprobe}")
        print("VRM preview: Qt WebEngine module import OK")
        return 0

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

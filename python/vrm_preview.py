"""Qt WebEngine host for the bundled, offline three-vrm character preview."""
from __future__ import annotations

import base64
import binascii
import functools
import http.server
import json
import math
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from vrm_mocap import normalize_blend_shapes, normalize_quaternion, vmc_bone_to_vrm


class _ViewerHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, directory: Path, model_route: str):
        self.model_path: Path | None = None
        self.model_route = model_route
        handler = functools.partial(_ViewerRequestHandler, directory=str(directory))
        super().__init__(("127.0.0.1", 0), handler)


class _ViewerRequestHandler(http.server.SimpleHTTPRequestHandler):
    server: _ViewerHttpServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path == self.server.model_route:
            source = self.server.model_path
            if source is None or not source.is_file():
                self.send_error(http.server.HTTPStatus.NOT_FOUND)
                return
            try:
                size = source.stat().st_size
                self.send_response(http.server.HTTPStatus.OK)
                self.send_header("Content-Type", "model/gltf-binary")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile)
            except (OSError, BrokenPipeError):
                return
            return
        super().do_GET()

    def log_message(self, _format: str, *_args) -> None:
        # Do not leak local model names or viewer traffic into application logs.
        return


def _viewer_index_path() -> Path | None:
    frozen_root = getattr(sys, "_MEIPASS", None)
    candidates = []
    if frozen_root:
        candidates.append(Path(frozen_root) / "vrm_viewer")
    candidates.extend((
        Path(__file__).resolve().parent / "resources" / "vrm_viewer",
        Path(__file__).resolve().parent.parent / "web" / "vrm_viewer" / "dist",
    ))
    for candidate in candidates:
        index = candidate / "index.html"
        if index.is_file():
            return index.resolve()
    return None


class VRMViewerWidget(QWidget):
    """Display local VRM 0.x/1.0 files and drive their normalized humanoid."""

    _export_frame_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self._page_ready = False
        self._ready_probe_count = 0
        self._bones: dict[str, dict] = {}
        self._expressions: dict[str, float] = {}
        self._timeline_state: dict | None = None
        self._server: _ViewerHttpServer | None = None
        self._server_thread: threading.Thread | None = None
        self._viewer_origin: str | None = None
        self._web_view: QWebEngineView | None = None
        self._pending_model_path: Path | None = None
        self._model_url: str | None = None
        self._model_route = self._new_model_route()

        self.status_label = QLabel("3Dビューアは必要になったときに起動します")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_button = QPushButton("VRMを開く...")
        self.close_button = QPushButton("モデルを閉じる")
        self.close_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_model_dialog)
        self.close_button.clicked.connect(self.close_model)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.close_button)
        toolbar.addStretch(1)

        self._placeholder = QLabel("VRMモデルを読み込むと、ここに3Dキャラクターを表示します")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color: #9da7b7; background: #17191f;")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.addLayout(toolbar)
        self._layout.addWidget(self._placeholder, stretch=1)
        self._layout.addWidget(self.status_label)

        self._state_timer = QTimer(self)
        self._state_timer.setInterval(33)
        self._state_timer.timeout.connect(self._flush_mocap_state)
        self._export_frame_requested.connect(self._handle_export_frame_request)

    @property
    def model_path(self) -> Path | None:
        return self._pending_model_path

    @staticmethod
    def _new_model_route() -> str:
        return "/__axral_model__/" + secrets.token_urlsafe(24) + ".vrm"

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._ensure_viewer()

    def _ensure_viewer(self) -> bool:
        # The hidden 3D dock is cheap: create Chromium and the loopback server
        # only when the user actually opens the 3D preview.
        if self._web_view is not None:
            return self._server is not None

        index = _viewer_index_path()
        if index is None:
            self.status_label.setText(
                "VRMビューア素材がありません。web/vrm_viewer で npm install と npm run build を実行してください。"
            )
            return False

        try:
            server = _ViewerHttpServer(index.parent, self._model_route)
            thread = threading.Thread(
                target=server.serve_forever,
                name="AxralVrmViewerHttp",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._server_thread = thread
            address, port = server.server_address

            web_view = QWebEngineView(self)
            settings = web_view.settings()
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
            )
            settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
            web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            web_view.setStyleSheet("background: #17191f;")
            web_view.titleChanged.connect(self._on_page_title_changed)
            web_view.loadFinished.connect(self._on_page_loaded)

            self._viewer_origin = f"http://{address}:{port}"
            self._web_view = web_view
            self._layout.insertWidget(1, web_view, stretch=1)
            self._placeholder.hide()

            if self._pending_model_path is not None:
                server.model_path = self._pending_model_path
                self._model_url = self._viewer_origin + self._model_route
            web_view.load(QUrl(self._viewer_origin + "/index.html"))
            self.status_label.setText("VRMビューアを起動しています...")
            return True
        except (OSError, RuntimeError) as exc:
            self.status_label.setText(f"VRMビューアを起動できません: {exc}")
            self._stop_server()
            return False

    def open_model_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "VRMモデルを選択", "", "VRMモデル (*.vrm)"
        )
        if path:
            self.load_model(path)

    def load_model(self, path: str | Path) -> None:
        source = Path(path).expanduser()
        if source.suffix.lower() != ".vrm":
            self.status_label.setText("VRM 0.x / 1.0 の .vrm ファイルを選択してください")
            return
        try:
            resolved = source.resolve(strict=True)
            if not resolved.is_file():
                raise OSError("ファイルではありません")
            size = resolved.stat().st_size
            if size <= 0 or size > 1024 * 1024 * 1024:
                raise ValueError("VRMファイルのサイズは1GiB以下にしてください")
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"VRMを開けません: {exc}")
            return

        if self._pending_model_path == resolved and self._model_url is not None:
            return
        self._pending_model_path = resolved
        self._timeline_state = None
        self._ready_probe_count = 0
        if not self._ensure_viewer():
            return
        assert self._server is not None and self._viewer_origin is not None
        # A fresh route prevents a previous in-flight load from observing a
        # newly selected file when the user switches models quickly.
        self._model_route = self._new_model_route()
        self._server.model_route = self._model_route
        self._server.model_path = resolved
        self._model_url = self._viewer_origin + self._model_route
        self._send_model_when_ready()

    def close_model(self) -> None:
        self._pending_model_path = None
        self._model_url = None
        self._timeline_state = None
        if self._server is not None:
            self._server.model_path = None
            self._server.model_route = self._new_model_route()
        if self._page_ready and self._web_view is not None:
            self._web_view.page().runJavaScript("window.axralVrmViewer?.closeModel();")
        self.close_button.setEnabled(False)
        self.status_label.setText("VRMモデルを閉じました")

    def set_head_quaternion(
        self,
        offset_x: float,
        offset_y: float,
        offset_z: float,
        qx: float,
        qy: float,
        qz: float,
        qw: float,
    ) -> None:
        try:
            rotation = normalize_quaternion((qx, qy, qz, qw))
            position = tuple(float(v) for v in (offset_x, offset_y, offset_z))
            if not all(math.isfinite(v) for v in position):
                return
        except (TypeError, ValueError, OverflowError):
            return
        self._bones["head"] = {"rotation": rotation, "position": position}

    def set_bone_transform(
        self, bone_name: str, qx: float, qy: float, qz: float, qw: float
    ) -> None:
        """Compatibility entry point for rotation-only sources."""
        normalized_name = vmc_bone_to_vrm(bone_name)
        if normalized_name is None or normalized_name == "head":
            return
        try:
            rotation = normalize_quaternion((qx, qy, qz, qw))
        except (TypeError, ValueError, OverflowError):
            return
        self._bones[normalized_name] = {"rotation": rotation}

    def set_bone_transform_3d(
        self,
        bone_name: str,
        x: float,
        y: float,
        z: float,
        qx: float,
        qy: float,
        qz: float,
        qw: float,
    ) -> None:
        normalized_name = vmc_bone_to_vrm(bone_name)
        if normalized_name is None or normalized_name == "head":
            return
        try:
            position = tuple(float(v) for v in (x, y, z))
            rotation = normalize_quaternion((qx, qy, qz, qw))
            if not all(math.isfinite(v) for v in position):
                return
        except (TypeError, ValueError, OverflowError):
            return
        transform = {"rotation": rotation}
        # VMC bone positions are world/model positions. Only the root Hips
        # translation is applied; child positions would overwrite each VRM's
        # own bone lengths and hierarchy. Other mapped bones receive rotation.
        if normalized_name == "hips":
            transform["position"] = position
        self._bones[normalized_name] = transform

    def set_blend_shapes(self, values: dict) -> None:
        try:
            self._expressions = normalize_blend_shapes(values)
        except (TypeError, ValueError, OverflowError):
            return

    def set_timeline_state(self, state: dict | None) -> None:
        """Set a sampled timeline pose; it takes priority over live VMC input."""
        if state is None:
            self._timeline_state = None
            return
        bones = state.get("bones", {})
        expressions = state.get("expressions", {})
        if not isinstance(bones, dict) or not isinstance(expressions, dict):
            return
        self._timeline_state = {"bones": bones, "expressions": expressions}

    def _on_page_loaded(self, success: bool) -> None:
        self._page_ready = bool(success)
        if not success:
            self.status_label.setText("VRMビューアの読み込みに失敗しました")
            return
        self._ready_probe_count = 0
        self._send_model_when_ready()
        if not self._state_timer.isActive():
            self._state_timer.start()

    def _on_page_title_changed(self, title: str) -> None:
        prefix = "AXRAL_VRM_STATUS:"
        if title.startswith(prefix):
            self.status_label.setText(title[len(prefix):])

    def _send_model_when_ready(self) -> None:
        if not self._page_ready or not self._model_url:
            return
        self._ready_probe_count += 1
        if self._web_view is None:
            return
        self._web_view.page().runJavaScript(
            "typeof window.axralVrmViewer?.loadModel === 'function'",
            self._on_viewer_api_probe,
        )

    def _on_viewer_api_probe(self, ready) -> None:
        if ready and self._model_url and self._web_view is not None:
            url = json.dumps(self._model_url)
            self._web_view.page().runJavaScript(
                "window.axralVrmViewer.loadModel(" + url + ");"
            )
            self.close_button.setEnabled(True)
            self.status_label.setText("VRMを読み込んでいます...")
            return
        if self._ready_probe_count < 30:
            QTimer.singleShot(100, self._send_model_when_ready)
        else:
            self.status_label.setText("VRMビューアの初期化に失敗しました")

    def render_frame_png(
        self,
        model_path: str | Path,
        pose: dict,
        frame_rate: float,
        *,
        timeout_seconds: float = 120.0,
    ) -> bytes:
        """Render one transparent VRM PNG frame from a non-GUI export worker.

        The worker waits on a threading.Event while the GUI thread performs the
        WebEngine operation. No QWidget or QWebEngine object is touched by the
        worker thread.
        """
        try:
            fps = float(frame_rate)
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("VRM render frame rate and timeout must be numeric") from exc
        if not math.isfinite(fps) or not 1.0 <= fps <= 240.0:
            raise ValueError("VRM render frame rate must be between 1 and 240")
        if not math.isfinite(timeout) or not 1.0 <= timeout <= 600.0:
            raise ValueError("VRM frame render timeout must be between 1 and 600 seconds")
        if not isinstance(pose, dict):
            raise ValueError("VRM render pose must be an object")

        request = {
            "model_path": str(Path(model_path).expanduser().resolve(strict=True)),
            "pose": pose,
            "frame_rate": fps,
            "deadline": time.monotonic() + timeout,
            "event": threading.Event(),
            "result": None,
            "error": None,
        }
        self._export_frame_requested.emit(request)
        if not request["event"].wait(timeout + 1.0):
            raise TimeoutError("VRM frame renderer did not respond before the timeout")
        if request["error"] is not None:
            raise RuntimeError(str(request["error"]))
        result = request["result"]
        if not isinstance(result, bytes) or not result.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("VRM frame renderer returned an invalid PNG image")
        return result

    @staticmethod
    def _finish_export_frame_request(request: dict, *, result=None, error=None) -> None:
        if request["event"].is_set():
            return
        request["result"] = result
        request["error"] = error
        request["event"].set()

    def _handle_export_frame_request(self, request: dict) -> None:
        """GUI-thread entry point for a blocking frame request from an export worker."""
        if request["event"].is_set():
            return
        if time.monotonic() >= request["deadline"]:
            self._finish_export_frame_request(
                request, error="Timed out while preparing the VRM preview"
            )
            return
        if not self._ensure_viewer():
            self._finish_export_frame_request(
                request, error=self.status_label.text()
            )
            return

        target_path = Path(request["model_path"])
        if self.model_path != target_path or self._model_url is None:
            self.load_model(target_path)
        if not self._page_ready or self._web_view is None or self._model_url is None:
            QTimer.singleShot(
                40, lambda current=request: self._handle_export_frame_request(current)
            )
            return

        self._web_view.page().runJavaScript(
            "window.axralVrmViewer?.getModelStatus()",
            lambda status, current=request: self._on_export_model_status(current, status),
        )

    def _on_export_model_status(self, request: dict, status) -> None:
        if request["event"].is_set():
            return
        if time.monotonic() >= request["deadline"]:
            self._finish_export_frame_request(
                request, error="Timed out while loading the VRM model for export"
            )
            return
        if not isinstance(status, dict):
            QTimer.singleShot(
                40, lambda current=request: self._handle_export_frame_request(current)
            )
            return
        load_error = status.get("error")
        if load_error:
            self._finish_export_frame_request(
                request, error=f"Could not load VRM model: {load_error}"
            )
            return
        if status.get("url") != self._model_url:
            QTimer.singleShot(
                40, lambda current=request: self._handle_export_frame_request(current)
            )
            return
        if self._web_view is None:
            self._finish_export_frame_request(
                request, error="VRM WebEngine view was closed during export"
            )
            return

        pose = json.dumps(request["pose"], allow_nan=False, separators=(",", ":"))
        fps = json.dumps(request["frame_rate"])
        script = (
            "window.axralVrmViewer.renderFrameDataUrl("
            + pose
            + ", 768, 768, 1 / "
            + fps
            + ")"
        )
        self._web_view.page().runJavaScript(
            script,
            lambda data_url, current=request: self._on_export_frame_rendered(
                current, data_url
            ),
        )

    def _on_export_frame_rendered(self, request: dict, data_url) -> None:
        if request["event"].is_set():
            return
        if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
            self._finish_export_frame_request(
                request, error="VRM WebEngine did not return a PNG frame"
            )
            return
        try:
            encoded = data_url.partition(",")[2]
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            self._finish_export_frame_request(
                request, error=f"Could not decode VRM PNG frame: {exc}"
            )
            return
        self._finish_export_frame_request(request, result=image)

    def _flush_mocap_state(self) -> None:
        if not self._page_ready or self._web_view is None:
            return
        state = (
            self._timeline_state
            if self._timeline_state is not None
            else {"bones": self._bones, "expressions": self._expressions}
        )
        try:
            payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        self._web_view.page().runJavaScript(
            "window.axralVrmViewer?.setMocapState(" + payload + ");"
        )

    def _stop_server(self) -> None:
        server = self._server
        self._server = None
        self._viewer_origin = None
        if server is not None:
            try:
                server.shutdown()
            except (OSError, RuntimeError):
                pass
            server.server_close()
        thread = self._server_thread
        self._server_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def shutdown(self) -> None:
        """Stop WebEngine activity and release the loopback-only HTTP server."""
        self._state_timer.stop()
        if self._web_view is not None:
            self._web_view.stop()
        self._stop_server()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

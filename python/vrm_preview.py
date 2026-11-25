"""Qt WebEngine host for the bundled, offline three-vrm character preview."""
from __future__ import annotations

import functools
import http.server
import json
import math
import secrets
import shutil
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QTimer, QUrl, Qt
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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self._page_ready = False
        self._ready_probe_count = 0
        self._bones: dict[str, dict] = {}
        self._expressions: dict[str, float] = {}
        self._server: _ViewerHttpServer | None = None
        self._server_thread: threading.Thread | None = None
        self._model_route = "/__axral_model__/" + secrets.token_urlsafe(24) + ".vrm"
        self._model_url: str | None = None

        self.status_label = QLabel("VRMモデルを読み込んでください")
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

        self.web_view = QWebEngineView(self)
        settings = self.web_view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.web_view.setStyleSheet("background: #17191f;")
        self.web_view.titleChanged.connect(self._on_page_title_changed)
        self.web_view.loadFinished.connect(self._on_page_loaded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(toolbar)
        layout.addWidget(self.web_view, stretch=1)
        layout.addWidget(self.status_label)

        index = _viewer_index_path()
        if index is None:
            self.status_label.setText(
                "VRMビューア素材がありません。web/vrm_viewer で npm install と npm run build を実行してください。"
            )
            self.web_view.hide()
        else:
            try:
                self._server = _ViewerHttpServer(index.parent, self._model_route)
                self._server_thread = threading.Thread(
                    target=self._server.serve_forever,
                    name="AxralVrmViewerHttp",
                    daemon=True,
                )
                self._server_thread.start()
                address, port = self._server.server_address
                self._viewer_origin = f"http://{address}:{port}"
                self.web_view.load(QUrl(self._viewer_origin + "/index.html"))
            except OSError as exc:
                self.status_label.setText(f"VRMビューアを起動できません: {exc}")
                self.web_view.hide()
                self._stop_server()

        self._state_timer = QTimer(self)
        self._state_timer.setInterval(33)
        self._state_timer.timeout.connect(self._flush_mocap_state)

    def open_model_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "VRMモデルを選択", "", "VRMモデル (*.vrm)"
        )
        if path:
            self.load_model(path)

    def load_model(self, path: str | Path) -> None:
        if self._server is None:
            self.status_label.setText("VRMビューアHTTPサーバーが利用できません")
            return
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

        self._server.model_path = resolved
        self._model_url = self._viewer_origin + self._model_route
        self._ready_probe_count = 0
        self._send_model_when_ready()

    def close_model(self) -> None:
        self._model_url = None
        if self._server is not None:
            self._server.model_path = None
        self._bones.clear()
        self._expressions.clear()
        if self._page_ready:
            self.web_view.page().runJavaScript("window.axralVrmViewer?.closeModel();")
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
        normalized_name = vmc_bone_to_vrm(bone_name)
        if normalized_name is None or normalized_name == "head":
            return
        try:
            rotation = normalize_quaternion((qx, qy, qz, qw))
        except (TypeError, ValueError, OverflowError):
            return
        self._bones[normalized_name] = {"rotation": rotation}

    def set_blend_shapes(self, values: dict) -> None:
        try:
            self._expressions = normalize_blend_shapes(values)
        except (TypeError, ValueError, OverflowError):
            return

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
        self.web_view.page().runJavaScript(
            "typeof window.axralVrmViewer?.loadModel === 'function'",
            self._on_viewer_api_probe,
        )

    def _on_viewer_api_probe(self, ready) -> None:
        if ready and self._model_url:
            url = json.dumps(self._model_url)
            self.web_view.page().runJavaScript(
                "window.axralVrmViewer.loadModel(" + url + ");"
            )
            self.close_button.setEnabled(True)
            self.status_label.setText("VRMを読み込んでいます...")
            return
        if self._ready_probe_count < 30:
            QTimer.singleShot(100, self._send_model_when_ready)
        else:
            self.status_label.setText("VRMビューアの初期化に失敗しました")

    def _flush_mocap_state(self) -> None:
        if not self._page_ready:
            return
        state = {"bones": self._bones, "expressions": self._expressions}
        try:
            payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        self.web_view.page().runJavaScript(
            "window.axralVrmViewer?.setMocapState(" + payload + ");"
        )

    def _stop_server(self) -> None:
        server = self._server
        self._server = None
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

    def closeEvent(self, event) -> None:
        self._state_timer.stop()
        self._stop_server()
        super().closeEvent(event)

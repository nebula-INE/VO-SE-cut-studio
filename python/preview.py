"""aural Studio - PySide6プレビュープレイヤー(骨組み)

C++で実装したVideoDecoder(pybind11経由)からRGBフレームをnumpy配列として
受け取り、QImageに変換してQLabelに描画する最小構成のプレビュープレイヤー。

使い方:
    python3 preview.py <video_file>
"""

from __future__ import annotations

import sys

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import aural_engine


class VideoPreviewWidget(QLabel):
    """デコードしたフレームを表示するだけの薄いラッパー。"""

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #202020;")
        self.setMinimumSize(320, 240)

    def show_frame(self, frame: np.ndarray) -> None:
        # frame: (H, W, 3) uint8, RGB順。
        # numpy配列のメモリをQImageが直接参照するため、C-contiguousで
        # なければならない(VideoDecoder側は常にtightly packedで返すため
        # 通常は問題ないが、念のため保証しておく)。
        frame = np.ascontiguousarray(frame)
        height, width, _ = frame.shape
        bytes_per_line = width * 3

        image = QImage(
            frame.tobytes(),  # QImageに独立したコピーを持たせ、frameの寿命に依存しないようにする
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        )
        pixmap = QPixmap.fromImage(image)

        # ウィジェットサイズに合わせてアスペクト比を保ったまま縮小表示
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


class PreviewWindow(QMainWindow):
    def __init__(self, video_path: str) -> None:
        super().__init__()
        self.setWindowTitle(f"aural preview - {video_path}")

        self.decoder = aural_engine.VideoDecoder()
        if not self.decoder.open(video_path):
            raise RuntimeError(f"Failed to open video: {video_path}")

        self.preview = VideoPreviewWidget()
        self.play_button = QPushButton("Pause")
        self.play_button.clicked.connect(self.toggle_playback)

        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(self.preview, stretch=1)
        layout.addLayout(controls)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.resize(self.decoder.width, self.decoder.height + 40)

        # fpsに応じたタイマー間隔でフレームを送り出す(簡易実装。
        # 本格的な同期はPhase後半で音声クロック基準に作り直す想定)。
        interval_ms = int(1000.0 / self.decoder.fps) if self.decoder.fps > 0 else 33
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.advance_frame)
        self.timer.start()

        self.is_playing = True

    def advance_frame(self) -> None:
        result = self.decoder.decode_next_frame()
        if result is None:
            # 終端に達したら先頭へシークしてループ再生
            self.decoder.seek(0.0)
            result = self.decoder.decode_next_frame()
            if result is None:
                return

        frame, _pts_seconds = result
        self.preview.show_frame(frame)

    def toggle_playback(self) -> None:
        if self.is_playing:
            self.timer.stop()
            self.play_button.setText("Play")
        else:
            self.timer.start()
            self.play_button.setText("Pause")
        self.is_playing = not self.is_playing

    def closeEvent(self, event) -> None:  # noqa: N802 (Qtの命名規則に合わせる)
        self.timer.stop()
        self.decoder.close()
        super().closeEvent(event)


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <video_file>", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    window = PreviewWindow(sys.argv[1])
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

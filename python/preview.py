"""aural Studio - 映像プレビューパネル

C++で実装したVideoDecoder(pybind11経由)からRGBフレームをnumpy配列として
受け取り、QImageに変換してQLabelに描画する最小構成のプレビュー。

PreviewPanel は他のウィンドウに埋め込んで使う再利用可能なQWidget。
このファイル単体では、PreviewPanelをQMainWindowでラップしただけの
簡易プレイヤーとして動作する(動作確認・単体デバッグ用)。

使い方(単体実行):
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
        self.setStyleSheet("background-color: #202020; color: #888888;")
        self.setMinimumSize(320, 240)
        self.setText("動画が読み込まれていません")

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


class PreviewPanel(QWidget):
    """動画を開いて再生プレビューする、埋め込み可能なパネル。

    他のウィンドウ(main_window.py等)から:
        panel = PreviewPanel()
        panel.open_video("bg.mp4")
    のように使う。video_pathを指定せずに構築した場合は、
    open_video()を呼ぶまでプレースホルダー表示のまま待機する。
    """

    def __init__(self, video_path: str | None = None) -> None:
        super().__init__()

        self.decoder: aural_engine.VideoDecoder | None = None
        self.is_playing = False

        self.preview = VideoPreviewWidget()
        self.play_button = QPushButton("Pause")
        self.play_button.clicked.connect(self.toggle_playback)
        self.play_button.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(self.preview, stretch=1)
        layout.addLayout(controls)
        self.setLayout(layout)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance_frame)

        if video_path:
            self.open_video(video_path)

    def open_video(self, video_path: str) -> bool:
        """動画ファイルを開いて再生を開始する。失敗時はFalseを返す。"""
        self.close_video()

        decoder = aural_engine.VideoDecoder()
        if not decoder.open(video_path):
            self.preview.setText(f"動画を開けませんでした: {video_path}")
            return False

        self.decoder = decoder
        self.play_button.setEnabled(True)

        interval_ms = int(1000.0 / decoder.fps) if decoder.fps > 0 else 33
        self.timer.setInterval(interval_ms)
        self.timer.start()
        self.is_playing = True
        self.play_button.setText("Pause")
        return True

    def close_video(self) -> None:
        """再生中の動画があれば停止してリソースを解放する。"""
        self.timer.stop()
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None
        self.is_playing = False
        self.play_button.setEnabled(False)

    def advance_frame(self) -> None:
        if self.decoder is None:
            return

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
        if self.decoder is None:
            return
        if self.is_playing:
            self.timer.stop()
            self.play_button.setText("Play")
        else:
            self.timer.start()
            self.play_button.setText("Pause")
        self.is_playing = not self.is_playing

    def cleanup(self) -> None:
        """ウィンドウが閉じられる際に呼び出す。"""
        self.close_video()


class PreviewWindow(QMainWindow):
    """PreviewPanelを単体で動かすための簡易ウィンドウ(動作確認用)。"""

    def __init__(self, video_path: str) -> None:
        super().__init__()
        self.setWindowTitle(f"aural preview - {video_path}")

        self.panel = PreviewPanel(video_path)
        self.setCentralWidget(self.panel)
        self.resize(640, 520)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qtの命名規則に合わせる)
        self.panel.cleanup()
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

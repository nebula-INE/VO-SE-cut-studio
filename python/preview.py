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
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
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

try:
    from character_renderer import render_character_frame
    _CHARACTER_RENDERER_AVAILABLE = True
except ImportError:
    _CHARACTER_RENDERER_AVAILABLE = False


class VideoPreviewWidget(QLabel):
    """デコードしたフレームを表示し、テロップ(字幕)をオーバーレイするウィジェット。

    テロップのテキストは台本エディタ側(ScriptEditorPanel)の推定タイミングから
    渡される想定(MainWindow経由)。VO-SE統合前のため実際の音声とは連動しない、
    タイミング確認用のプレビュー。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #202020; color: #888888;")
        self.setMinimumSize(320, 240)
        self.setText("動画が読み込まれていません")

        self._last_frame: np.ndarray | None = None
        self._telop_text: str = ""

        # --- リップシンクキャラクターまわりの状態 ---
        self._mouth_openness: float = 0.0
        self._character_enabled: bool = False  # 最初にset_mouth_opennessが呼ばれたら有効化
        self._character_start_time: float = time.monotonic()  # 微動アニメーションの基準時刻

        # --- モーションキャプチャ(頭部トラッキング)の状態 ---
        self._head_offset_x: float = 0.0
        self._head_offset_y: float = 0.0
        self._head_tilt_deg: float = 0.0

    def show_frame(self, frame: np.ndarray) -> None:
        # frame: (H, W, 3) uint8, RGB順。
        # numpy配列のメモリをQImageが直接参照するため、C-contiguousで
        # なければならない(VideoDecoder側は常にtightly packedで返すため
        # 通常は問題ないが、念のため保証しておく)。
        self._last_frame = np.ascontiguousarray(frame)
        self._redraw()

    def set_telop(self, text: str) -> None:
        """現在表示すべきテロップのテキストを設定する。空文字列で非表示。"""
        if text == self._telop_text:
            return
        self._telop_text = text
        # 動画が一時停止中でもテロップの更新だけは画面に反映させる
        self._redraw()

    def set_mouth_openness(self, value: float) -> None:
        """台本エディタ側の音声再生位置から算出された、現在の口の開き具合
        (0.0〜1.0)を受け取る。初回呼び出し時にキャラクターオーバーレイを
        有効化する(それまでは動画に何も重ねない)。
        """
        self._character_enabled = True
        self._mouth_openness = max(0.0, min(1.0, value))
        # 動画が一時停止中でも口の動きだけは画面に反映させる
        self._redraw()

    def set_head_transform(self, offset_x: float, offset_y: float, tilt_deg: float) -> None:
        """OSC(VMCプロトコル)経由で受信した頭の位置・傾きを反映する。
        set_mouth_opennessと同様、初回呼び出し時にキャラクターオーバーレイを
        有効化する(モーションキャプチャだけを先に繋いだ場合にも動作する)。
        """
        self._character_enabled = True
        self._head_offset_x = offset_x
        self._head_offset_y = offset_y
        self._head_tilt_deg = tilt_deg
        self._redraw()

    def _redraw(self) -> None:
        if self._last_frame is None:
            return

        height, width, _ = self._last_frame.shape
        bytes_per_line = width * 3

        image = QImage(
            self._last_frame.tobytes(),  # QImageに独立したコピーを持たせる
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        )
        pixmap = QPixmap.fromImage(image)

        if self._character_enabled and _CHARACTER_RENDERER_AVAILABLE:
            self._draw_character(pixmap)

        if self._telop_text:
            self._draw_telop(pixmap)

        # ウィジェットサイズに合わせてアスペクト比を保ったまま縮小表示
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)

    def _draw_character(self, pixmap: QPixmap) -> None:
        """リップシンクキャラクターを、フレーム画像(元解像度)の左下に重ねる。"""
        time_sec = time.monotonic() - self._character_start_time
        character_img = render_character_frame(
            mouth_openness=self._mouth_openness,
            time_sec=time_sec,
            head_offset_x=self._head_offset_x,
            head_offset_y=self._head_offset_y,
            head_tilt_deg=self._head_tilt_deg,
        )

        # PIL(RGBA, tobytes)からQImageへ変換。PILの行の並びはQImageの
        # Format_RGBA8888と一致するため、追加の変換無しでそのまま渡せる。
        char_qimage = QImage(
            character_img.tobytes("raw", "RGBA"),
            character_img.width,
            character_img.height,
            QImage.Format.Format_RGBA8888,
        )
        char_pixmap = QPixmap.fromImage(char_qimage)

        # フレーム高さの40%程度になるようスケールし、左下に配置する
        target_height = int(pixmap.height() * 0.4)
        scaled_char = char_pixmap.scaledToHeight(
            target_height, Qt.TransformationMode.SmoothTransformation
        )

        painter = QPainter(pixmap)
        margin = int(pixmap.height() * 0.03)
        x = margin
        y = pixmap.height() - scaled_char.height() - margin
        painter.drawPixmap(x, y, scaled_char)
        painter.end()

    def _draw_telop(self, pixmap: QPixmap) -> None:
        """フレーム画像(元解像度)の下部に半透明バー+テキストを描画する。"""
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bar_height = max(28, int(pixmap.height() * 0.12))
        bar_rect = QRectF(0, pixmap.height() - bar_height, pixmap.width(), bar_height)

        painter.fillRect(bar_rect, QColor(0, 0, 0, 160))

        font = painter.font()
        font.setPointSize(max(10, int(bar_height * 0.4)))
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(bar_rect, Qt.AlignmentFlag.AlignCenter, self._telop_text)

        painter.end()


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

    def set_telop(self, text: str) -> None:
        """台本エディタ側から現在のセリフのテキストを受け取り、
        映像上にオーバーレイ表示する。空文字列で非表示になる。
        """
        self.preview.set_telop(text)

    def set_mouth_openness(self, value: float) -> None:
        """台本エディタ側の音声再生位置から算出された、現在の口の開き具合
        (0.0〜1.0)を受け取り、映像上のキャラクターに反映する。
        """
        self.preview.set_mouth_openness(value)

    def set_head_transform(self, offset_x: float, offset_y: float, tilt_deg: float) -> None:
        """OSC(VMCプロトコル)経由で受信した頭の位置・傾きを、
        映像上のキャラクターに反映する。
        """
        self.preview.set_head_transform(offset_x, offset_y, tilt_deg)

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

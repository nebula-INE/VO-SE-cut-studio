"""aural Studio - チャット型台本エディタ(骨組み)

話者を選んでセリフを入力すると、チャット風のトランスクリプト表示と、
下部のタイムライン(尺の目安つき)に自動で反映される。

現時点ではVO-SE(音声合成)エンジンが未統合のため、各セリフの「尺」は
文字数から概算した仮の値(estimated=True)を使っている。エンジン統合後は
実際の合成音声の長さに差し替える想定(TODO: マーク箇所を参照)。

使い方:
    python3 script_editor.py
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QFont, QBrush, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


# --- 話者読み上げ速度の概算値(日本語, 文字/秒) ---
# TODO(VO-SE統合後): この定数は廃止し、実際に合成した音声の長さを使う。
ESTIMATED_CHARS_PER_SECOND = 7.0
MIN_ESTIMATED_DURATION_SEC = 0.8


# --- データモデル ---

@dataclass
class Speaker:
    name: str
    color: str  # "#RRGGBB"


@dataclass
class ScriptLine:
    speaker_name: str
    text: str
    line_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def estimated_duration_sec(self) -> float:
        # TODO(VO-SE統合後): VO-SEが返す実際の音声長に置き換える。
        # それまでは文字数からの概算値(estimated)を使う。
        return max(MIN_ESTIMATED_DURATION_SEC, len(self.text) / ESTIMATED_CHARS_PER_SECOND)


class ScriptModel:
    """台本全体(話者一覧+セリフ一覧)を保持するデータモデル。"""

    def __init__(self) -> None:
        self.speakers: list[Speaker] = [
            Speaker(name="ナレーター", color="#4A90D9"),
            Speaker(name="キャラA", color="#D94A4A"),
            Speaker(name="キャラB", color="#4AD97A"),
        ]
        self.lines: list[ScriptLine] = []

    def add_speaker(self, name: str, color: str) -> None:
        self.speakers.append(Speaker(name=name, color=color))

    def speaker_by_name(self, name: str) -> Speaker | None:
        for s in self.speakers:
            if s.name == name:
                return s
        return None

    def add_line(self, speaker_name: str, text: str) -> ScriptLine:
        line = ScriptLine(speaker_name=speaker_name, text=text)
        self.lines.append(line)
        return line

    def remove_line(self, line_id: str) -> None:
        self.lines = [l for l in self.lines if l.line_id != line_id]

    def total_duration_sec(self) -> float:
        return sum(l.estimated_duration_sec for l in self.lines)

    def to_dict(self) -> dict:
        return {
            "speakers": [asdict(s) for s in self.speakers],
            "lines": [
                {"speaker_name": l.speaker_name, "text": l.text, "line_id": l.line_id}
                for l in self.lines
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScriptModel":
        model = cls()
        model.speakers = [Speaker(**s) for s in data.get("speakers", [])]
        model.lines = [
            ScriptLine(speaker_name=l["speaker_name"], text=l["text"], line_id=l.get("line_id", uuid.uuid4().hex[:8]))
            for l in data.get("lines", [])
        ]
        return model


# --- UI: チャット風の入力欄 ---

class ChatInputWidget(QWidget):
    """話者選択 + テキスト入力 + 送信ボタン。Enterキーでも送信できる。"""

    line_submitted = Signal(str, str)  # (speaker_name, text)

    def __init__(self, model: ScriptModel) -> None:
        super().__init__()
        self.model = model

        self.speaker_combo = QComboBox()
        self._refresh_speakers()

        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("セリフを入力してEnter...")
        self.text_input.returnPressed.connect(self._submit)

        self.send_button = QPushButton("追加")
        self.send_button.clicked.connect(self._submit)

        layout = QHBoxLayout()
        layout.addWidget(self.speaker_combo)
        layout.addWidget(self.text_input, stretch=1)
        layout.addWidget(self.send_button)
        self.setLayout(layout)

    def _refresh_speakers(self) -> None:
        self.speaker_combo.clear()
        for speaker in self.model.speakers:
            self.speaker_combo.addItem(speaker.name)

    def _submit(self) -> None:
        text = self.text_input.text().strip()
        if not text:
            return
        speaker_name = self.speaker_combo.currentText()
        self.line_submitted.emit(speaker_name, text)
        self.text_input.clear()


# --- UI: チャット風トランスクリプト表示 ---

class TranscriptWidget(QListWidget):
    """セリフを話者ごとに色分けした吹き出し風リストで表示する。"""

    def __init__(self, model: ScriptModel) -> None:
        super().__init__()
        self.model = model
        self.setStyleSheet("QListWidget { background-color: #2b2b2b; border: none; }")
        self.setSpacing(4)

    def add_line_item(self, line: ScriptLine) -> None:
        speaker = self.model.speaker_by_name(line.speaker_name)
        color = speaker.color if speaker else "#888888"

        item = QListWidgetItem()
        label = QLabel(f"<b style='color:{color}'>{line.speaker_name}</b><br>{line.text}")
        label.setWordWrap(True)
        label.setStyleSheet(
            "background-color: #3c3c3c; border-radius: 8px; padding: 8px; color: #eeeeee;"
        )
        item.setSizeHint(label.sizeHint())
        self.addItem(item)
        self.setItemWidget(item, label)
        self.scrollToBottom()


# --- UI: タイムライン(尺の目安を横並びブロックで表示) ---

class TimelineWidget(QWidget):
    """セリフを尺の目安に応じた横幅のブロックとして時系列に並べる。

    現時点ではVO-SEが無いため、ブロック幅は文字数からの概算値(estimated)。
    実際の音声合成が入った際は、ScriptLine.estimated_duration_secの参照元を
    差し替えるだけでこのウィジェットはそのまま動く設計にしている。
    """

    PIXELS_PER_SECOND = 40
    BLOCK_HEIGHT = 56

    def __init__(self, model: ScriptModel) -> None:
        super().__init__()
        self.model = model
        self.setMinimumHeight(self.BLOCK_HEIGHT + 20)
        self.setStyleSheet("background-color: #1e1e1e;")

    def sizeHint(self):
        from PySide6.QtCore import QSize
        total_width = int(self.model.total_duration_sec() * self.PIXELS_PER_SECOND) + 40
        return QSize(max(total_width, 400), self.BLOCK_HEIGHT + 20)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qtの命名規則に合わせる)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        x = 10
        y = 10
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)

        for line in self.model.lines:
            speaker = self.model.speaker_by_name(line.speaker_name)
            color = QColor(speaker.color if speaker else "#888888")
            width = max(20, int(line.estimated_duration_sec * self.PIXELS_PER_SECOND))

            painter.setBrush(QBrush(color))
            painter.setPen(QPen(color.darker(150), 1))
            painter.drawRoundedRect(x, y, width, self.BLOCK_HEIGHT, 6, 6)

            painter.setPen(QPen(QColor("#ffffff")))
            text_rect_width = width - 8
            elided = painter.fontMetrics().elidedText(
                line.text, Qt.TextElideMode.ElideRight, max(text_rect_width, 0)
            )
            painter.drawText(x + 4, y + self.BLOCK_HEIGHT // 2 + 4, elided)

            x += width + 6

        painter.end()


# --- メインウィンドウ ---

class ScriptEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("aural Studio - 台本エディタ")
        self.resize(900, 600)

        self.model = ScriptModel()

        self.transcript = TranscriptWidget(self.model)
        self.chat_input = ChatInputWidget(self.model)
        self.chat_input.line_submitted.connect(self.on_line_submitted)

        timeline_scroll = QScrollArea()
        self.timeline = TimelineWidget(self.model)
        timeline_scroll.setWidget(self.timeline)
        timeline_scroll.setWidgetResizable(False)
        timeline_scroll.setFixedHeight(self.timeline.BLOCK_HEIGHT + 40)
        timeline_scroll.setStyleSheet("background-color: #1e1e1e; border: none;")

        chat_panel = QWidget()
        chat_layout = QVBoxLayout()
        chat_layout.addWidget(self.transcript, stretch=1)
        chat_layout.addWidget(self.chat_input)
        chat_panel.setLayout(chat_layout)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(chat_panel)
        splitter.addWidget(timeline_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        self.duration_label = QLabel()
        self._update_duration_label()

        toolbar = QHBoxLayout()
        save_button = QPushButton("台本を保存")
        save_button.clicked.connect(self.save_script)
        load_button = QPushButton("台本を開く")
        load_button.clicked.connect(self.load_script)
        toolbar.addWidget(save_button)
        toolbar.addWidget(load_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.duration_label)

        central = QWidget()
        central_layout = QVBoxLayout()
        central_layout.addLayout(toolbar)
        central_layout.addWidget(splitter, stretch=1)
        central.setLayout(central_layout)
        self.setCentralWidget(central)

    def on_line_submitted(self, speaker_name: str, text: str) -> None:
        line = self.model.add_line(speaker_name, text)
        self.transcript.add_line_item(line)
        self.timeline.updateGeometry()
        self.timeline.update()
        self._update_duration_label()

    def _update_duration_label(self) -> None:
        total = self.model.total_duration_sec()
        self.duration_label.setText(f"推定尺(仮): {total:.1f}秒 ※VO-SE統合前の概算値")

    def save_script(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "台本を保存", "", "JSON (*.json)")
        if not path:
            return
        Path(path).write_text(
            json.dumps(self.model.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "台本を開く", "", "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.model = ScriptModel.from_dict(data)
        except Exception as e:  # noqa: BLE001 (UIへのエラー表示が目的)
            QMessageBox.critical(self, "読み込みエラー", str(e))
            return

        self.transcript.clear()
        self.transcript.model = self.model
        for line in self.model.lines:
            self.transcript.add_line_item(line)

        self.chat_input.model = self.model
        self.chat_input._refresh_speakers()

        self.timeline.model = self.model
        self.timeline.updateGeometry()
        self.timeline.update()
        self._update_duration_label()


def main() -> int:
    app = QApplication(sys.argv)
    window = ScriptEditorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

"""aural Studio - チャット型台本エディタ(骨組み)

話者を選んでセリフを入力すると、チャット風のトランスクリプト表示と、
下部のタイムライン(尺の目安つき)に自動で反映される。

現時点ではVO-SE(音声合成)エンジンが未統合のため、各セリフの「尺」は
文字数から概算した仮の値(estimated=True)を使っている。エンジン統合後は
実際の合成音声の長さに差し替える想定(TODO: マーク箇所を参照)。

ScriptEditorPanel は他のウィンドウに埋め込んで使う再利用可能なQWidget。
このファイル単体では、ScriptEditorPanelをQMainWindowでラップしただけの
簡易エディタとして動作する(動作確認・単体デバッグ用)。

使い方(単体実行):
    python3 script_editor.py
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QFont, QBrush, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
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

try:
    from vose_worker import SynthesisLine, start_synthesis
    from lipsync import extract_mouth_envelope, mouth_openness_at
    _VOSE_AVAILABLE = True
except ImportError:
    _VOSE_AVAILABLE = False


# --- 話者読み上げ速度の概算値(日本語, 文字/秒) ---
# TODO(VO-SE統合後): この定数は廃止し、実際に合成した音声の長さを使う。
ESTIMATED_CHARS_PER_SECOND = 7.0
MIN_ESTIMATED_DURATION_SEC = 0.8


# --- データモデル ---

@dataclass
class Speaker:
    name: str
    color: str  # "#RRGGBB"
    # "openjtalk": 素の読み上げ(pyopenjtalk.tts()を直接使う、VO-SE非経由)。
    # それ以外の値: UTAU形式ボイスバンクの識別子として扱い、VO-SE(vose_core)
    # 経由で合成する。歌声合成(旧ロードマップの「[歌唱]タグ連携」相当)は
    # VO-SE自体の機能と重複するため、専用タグとしては別立てせず、話者に
    # UTAU音源を割り当てるだけで自然に扱えるようにしている。
    voice_source: str = "openjtalk"


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
            Speaker(name="ナレーター", color="#4A90D9", voice_source="openjtalk"),
            Speaker(name="キャラA", color="#D94A4A", voice_source="openjtalk"),
            Speaker(name="キャラB(UTAU音源)", color="#4AD97A", voice_source="test_voicebank"),
        ]
        self.lines: list[ScriptLine] = []

    def add_speaker(self, name: str, color: str, voice_source: str = "openjtalk") -> None:
        self.speakers.append(Speaker(name=name, color=color, voice_source=voice_source))

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

    def line_at_time(self, t: float) -> ScriptLine | None:
        """累積尺(各セリフのestimated_duration_secの積み上げ)から、
        経過時間tの時点で読み上げられているはずのセリフを返す。
        該当が無ければNone。テロップのプレビュー表示用。
        """
        if t < 0:
            return None
        elapsed = 0.0
        for line in self.lines:
            duration = line.estimated_duration_sec
            if elapsed <= t < elapsed + duration:
                return line
            elapsed += duration
        return None

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
        self.playhead_sec: float | None = None  # Noneなら再生カーソルを描画しない
        self.setMinimumHeight(self.BLOCK_HEIGHT + 20)
        self.setStyleSheet("background-color: #1e1e1e;")

    def set_playhead(self, t: float | None) -> None:
        self.playhead_sec = t
        self.update()

    def sizeHint(self) -> QSize:
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

        if self.playhead_sec is not None:
            playhead_x = 10 + int(self.playhead_sec * self.PIXELS_PER_SECOND)
            painter.setPen(QPen(QColor("#ffee00"), 2))
            painter.drawLine(playhead_x, 0, playhead_x, self.BLOCK_HEIGHT + 20)

        painter.end()


# --- 埋め込み可能なパネル本体 ---

class ScriptEditorPanel(QWidget):
    """台本編集(チャット入力+トランスクリプト+タイムライン)一式をまとめたパネル。

    他のウィンドウ(main_window.py等)から:
        panel = ScriptEditorPanel()
    のように埋め込んで使う。

    「▶ 台本プレビュー再生」を押すと、各セリフの推定尺(estimated_duration_sec)
    に従って内部クロックが進み、現在読み上げられているはずのセリフのテキストを
    telop_changed シグナルで発信する。VO-SEが無い現時点では実際の音声とは
    連動していない、あくまで「タイミングの確認用」のプレビュー。
    """

    telop_changed = Signal(str)  # 現在アクティブなセリフのテキスト(無ければ空文字列)
    mouth_openness_changed = Signal(float)  # 現在の再生位置での口の開き具合(0.0〜1.0)

    PLAYBACK_TICK_MS = 50

    def __init__(self, vose_lib_path: str | None = None) -> None:
        super().__init__()

        self.model = ScriptModel()
        self._playback_elapsed_sec = 0.0
        self._is_previewing = False

        # --- VO-SE音声合成まわりの状態 ---
        self._vose_lib_path = vose_lib_path
        self._synth_thread = None
        self._synth_wav_paths: dict[str, str] = {}   # line_id -> wavファイルパス
        self._synth_envelopes: dict[str, list] = {}  # line_id -> LipSyncFrameのリスト
        self._playback_queue: list[str] = []          # 再生待ちのline_idキュー
        self._synth_pending_count = 0
        self._current_playing_line_id: str | None = None

        self.audio_output = QAudioOutput()
        self.media_player = QMediaPlayer()
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.media_player.positionChanged.connect(self._on_media_position_changed)

        self.transcript = TranscriptWidget(self.model)
        self.chat_input = ChatInputWidget(self.model)
        self.chat_input.line_submitted.connect(self.on_line_submitted)

        self.timeline_scroll = QScrollArea()
        self.timeline = TimelineWidget(self.model)
        self.timeline_scroll.setWidget(self.timeline)
        self.timeline_scroll.setWidgetResizable(False)
        self.timeline_scroll.setFixedHeight(self.timeline.BLOCK_HEIGHT + 40)
        self.timeline_scroll.setStyleSheet("background-color: #1e1e1e; border: none;")

        chat_panel = QWidget()
        chat_layout = QVBoxLayout()
        chat_layout.addWidget(self.transcript, stretch=1)
        chat_layout.addWidget(self.chat_input)
        chat_panel.setLayout(chat_layout)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(chat_panel)
        splitter.addWidget(self.timeline_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        self.duration_label = QLabel()
        self._update_duration_label()

        self.preview_button = QPushButton("▶ 台本プレビュー再生")
        self.preview_button.clicked.connect(self.toggle_preview_playback)

        self.synth_button = QPushButton("🔊 音声合成して再生")
        self.synth_button.clicked.connect(self.start_voice_synthesis)
        self.synth_button.setEnabled(_VOSE_AVAILABLE)
        if not _VOSE_AVAILABLE:
            self.synth_button.setToolTip("vose_worker(VO-SEエンジン連携)が利用できません")

        self.synth_status_label = QLabel("")

        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(self.PLAYBACK_TICK_MS)
        self.playback_timer.timeout.connect(self._advance_playback)

        toolbar = QHBoxLayout()
        save_button = QPushButton("台本を保存")
        save_button.clicked.connect(self.save_script)
        load_button = QPushButton("台本を開く")
        load_button.clicked.connect(self.load_script)
        toolbar.addWidget(save_button)
        toolbar.addWidget(load_button)
        toolbar.addWidget(self.preview_button)
        toolbar.addWidget(self.synth_button)
        toolbar.addWidget(self.synth_status_label)
        toolbar.addStretch(1)
        toolbar.addWidget(self.duration_label)

        layout = QVBoxLayout()
        layout.addLayout(toolbar)
        layout.addWidget(splitter, stretch=1)
        self.setLayout(layout)

    def on_line_submitted(self, speaker_name: str, text: str) -> None:
        line = self.model.add_line(speaker_name, text)
        self.transcript.add_line_item(line)
        self.timeline.updateGeometry()
        self.timeline.update()
        self._update_duration_label()

    def toggle_preview_playback(self) -> None:
        if self._is_previewing:
            self.stop_preview_playback()
        else:
            self.start_preview_playback()

    # --- VO-SE音声合成 + 順次再生 ---

    def start_voice_synthesis(self) -> None:
        if not _VOSE_AVAILABLE:
            QMessageBox.warning(self, "音声合成", "vose_worker(VO-SEエンジン連携)が利用できません。")
            return
        if not self.model.lines:
            return
        if self._synth_thread is not None:
            return  # 既に合成中

        self.synth_button.setEnabled(False)
        self.synth_status_label.setText("合成中... (0/{})".format(len(self.model.lines)))

        self._synth_wav_paths.clear()
        self._playback_queue.clear()
        self._synth_pending_count = len(self.model.lines)

        lines = []
        for line in self.model.lines:
            speaker = self.model.speaker_by_name(line.speaker_name)
            voice_source = speaker.voice_source if speaker else "openjtalk"
            lines.append(SynthesisLine(line.line_id, line.text, voice_source))
        self._synth_thread = start_synthesis(
            self._vose_lib_path,
            lines,
            on_line_ready=self._on_synth_line_ready,
            on_line_failed=self._on_synth_line_failed,
            on_all_finished=self._on_synth_all_finished,
        )

    def _on_synth_line_ready(self, line_id: str, wav_path: str) -> None:
        self._synth_wav_paths[line_id] = wav_path
        try:
            self._synth_envelopes[line_id] = extract_mouth_envelope(wav_path)
        except Exception as e:  # noqa: BLE001 (エンベロープが無くてもテロップ/音声再生は継続する)
            print(f"[リップシンク] エンベロープ抽出に失敗 line_id={line_id}: {e}")
            self._synth_envelopes[line_id] = []
        self._playback_queue.append(line_id)
        self._synth_pending_count -= 1
        self.synth_status_label.setText(
            "合成中... ({}/{})".format(len(self.model.lines) - self._synth_pending_count, len(self.model.lines))
        )
        # 再生キューが空(=何も再生していない)状態なら、この行から再生を始める
        if self.media_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._play_next_in_queue()

    def _on_synth_line_failed(self, line_id: str, error_message: str) -> None:
        self._synth_pending_count -= 1
        print(f"[音声合成エラー] line_id={line_id}: {error_message}")

    def _on_synth_all_finished(self) -> None:
        self.synth_button.setEnabled(True)
        self.synth_status_label.setText("")
        if self._synth_thread is not None:
            # run()は既に返っているはずだが、QThreadオブジェクトを破棄する前に
            # 必ずwait()でOSスレッドの終了を確定させる(Qtの既定動作)。
            self._synth_thread.wait()
        self._synth_thread = None

    def _play_next_in_queue(self) -> None:
        if not self._playback_queue:
            return
        line_id = self._playback_queue.pop(0)
        wav_path = self._synth_wav_paths.get(line_id)
        if not wav_path:
            self._play_next_in_queue()
            return

        self._current_playing_line_id = line_id

        line = next((l for l in self.model.lines if l.line_id == line_id), None)
        if line:
            self.telop_changed.emit(line.text)

        self.media_player.setSource(QUrl.fromLocalFile(wav_path))
        self.media_player.play()

    def _on_media_position_changed(self, position_ms: int) -> None:
        """再生位置(ms)から、現在再生中の行のリップシンク用エンベロープを引いて
        口の開き具合をmouth_openness_changedとして発信する。
        """
        if self._current_playing_line_id is None:
            return
        frames = self._synth_envelopes.get(self._current_playing_line_id)
        if not frames:
            return
        openness = mouth_openness_at(frames, position_ms / 1000.0)
        self.mouth_openness_changed.emit(openness)

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._playback_queue:
                self._play_next_in_queue()
            else:
                self._current_playing_line_id = None
                self.telop_changed.emit("")  # 再生キューを使い切ったらテロップを消す
                self.mouth_openness_changed.emit(0.0)  # 口を閉じる

    def start_preview_playback(self) -> None:
        if not self.model.lines:
            return
        self._is_previewing = True
        self._playback_elapsed_sec = 0.0
        self.preview_button.setText("■ 再生停止")
        self.playback_timer.start()
        self._advance_playback()  # 最初のセリフを即座に反映する

    def stop_preview_playback(self) -> None:
        self._is_previewing = False
        self.playback_timer.stop()
        self.preview_button.setText("▶ 台本プレビュー再生")
        self.timeline.set_playhead(None)
        self.telop_changed.emit("")

    def _advance_playback(self) -> None:
        total = self.model.total_duration_sec()
        if total <= 0:
            self.stop_preview_playback()
            return

        # 台本全体を尺分ループ再生する(動画側のループ再生と発想を合わせている)
        self._playback_elapsed_sec = (self._playback_elapsed_sec + self.PLAYBACK_TICK_MS / 1000.0) % total

        self.timeline.set_playhead(self._playback_elapsed_sec)

        active_line = self.model.line_at_time(self._playback_elapsed_sec)
        self.telop_changed.emit(active_line.text if active_line else "")

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
        self.load_script_from_path(path)

    def load_script_from_path(self, path: str) -> bool:
        self.stop_preview_playback()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.model = ScriptModel.from_dict(data)
        except Exception as e:  # noqa: BLE001 (UIへのエラー表示が目的)
            QMessageBox.critical(self, "読み込みエラー", str(e))
            return False

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
        return True

    def cleanup(self) -> None:
        """ウィンドウが閉じられる際に呼び出す。"""
        self.playback_timer.stop()
        self.media_player.stop()
        if self._synth_thread is not None:
            self._synth_thread.cancel()
            self._synth_thread.quit()
            self._synth_thread.wait(3000)


class ScriptEditorWindow(QMainWindow):
    """ScriptEditorPanelを単体で動かすための簡易ウィンドウ(動作確認用)。"""

    def __init__(self, vose_lib_path: str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("aural Studio - 台本エディタ")
        self.resize(900, 600)

        self.panel = ScriptEditorPanel(vose_lib_path)
        self.setCentralWidget(self.panel)

    # ScriptEditorWindow.model / on_line_submitted 等への既存アクセス(テスト等)
    # との後方互換のため、panel側へ委譲するプロパティを用意しておく。
    @property
    def model(self) -> ScriptModel:
        return self.panel.model

    def on_line_submitted(self, speaker_name: str, text: str) -> None:
        self.panel.on_line_submitted(speaker_name, text)


def main() -> int:
    app = QApplication(sys.argv)
    window = ScriptEditorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

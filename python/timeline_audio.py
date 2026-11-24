"""Timeline audio preview driven by the editor's timeline clock.

Each enabled audio clip gets its own Qt Multimedia player. The platform audio
backend mixes concurrent players; this layer schedules, seeks, pauses, and
mutes them without touching the VO-SE synthesis or export pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from timeline_model import TimelineClip, TimelineModel, TimelineTrack


def active_audio_clips(
    model: TimelineModel, timeline_seconds: float
) -> list[tuple[TimelineClip, TimelineTrack]]:
    """Return enabled audio clips covering a timeline position, in stable order."""
    tracks = {track.id: track for track in model.tracks}
    active: list[tuple[TimelineClip, TimelineTrack]] = []
    for clip in model.clips:
        track = tracks.get(clip.track_id)
        if (
            track is None
            or track.kind != "audio"
            or track.muted
            or not clip.enabled
            or clip.kind not in {"audio", "voice"}
            or timeline_seconds < clip.start
            or timeline_seconds >= clip.end
        ):
            continue
        active.append((clip, track))
    return sorted(active, key=lambda item: (item[1].name, item[0].start, item[0].id))


@dataclass
class _PlayerEntry:
    player: QMediaPlayer
    output: QAudioOutput
    path: Path
    active: bool = False


class TimelineAudioPlayback(QObject):
    """Synchronize and mix timeline audio clips using Qt Multimedia players.

    The editor video PTS is the master clock during video playback. While the
    video transport is paused between clips, a monotonic clock keeps audio and
    the timeline playhead moving across the gap.
    """

    position_changed = Signal(float)
    error = Signal(str)

    TICK_MS = 20
    SEEK_DRIFT_MS = 300

    def __init__(
        self,
        model_provider: Callable[[], TimelineModel],
        asset_resolver: Callable[[TimelineClip], Path],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model_provider = model_provider
        self._asset_resolver = asset_resolver
        self._players: dict[str, _PlayerEntry] = {}
        self._failed_clips: set[str] = set()
        self._playing = False
        self._anchor_seconds = 0.0
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start(self, timeline_seconds: float) -> None:
        """Start the audio timeline clock at the requested timeline position."""
        self._anchor_seconds = max(0.0, float(timeline_seconds))
        self._clock.start()
        self._playing = True
        if not self._timer.isActive():
            self._timer.start()
        self._sync_players(self._anchor_seconds)
        self.position_changed.emit(self._anchor_seconds)

    def sync_to_timeline(self, timeline_seconds: float) -> None:
        """Re-anchor audio to a decoded video PTS or an explicit timeline seek."""
        if not self._playing:
            return
        self._anchor_seconds = max(0.0, float(timeline_seconds))
        self._clock.restart()
        self._sync_players(self._anchor_seconds)
        self.position_changed.emit(self._anchor_seconds)

    def pause(self) -> None:
        if not self._playing:
            return
        self._anchor_seconds = self._position_seconds()
        self._playing = False
        self._timer.stop()
        for entry in self._players.values():
            if entry.active:
                entry.player.pause()
                entry.active = False

    def stop(self) -> None:
        self._playing = False
        self._timer.stop()
        for entry in self._players.values():
            entry.player.stop()
            entry.player.setSource(QUrl())
            entry.player.deleteLater()
            entry.output.deleteLater()
        self._players.clear()
        self._failed_clips.clear()

    def _position_seconds(self) -> float:
        if not self._clock.isValid():
            return self._anchor_seconds
        return self._anchor_seconds + self._clock.elapsed() / 1000.0

    def _on_tick(self) -> None:
        if not self._playing:
            return
        timeline_seconds = self._position_seconds()
        self._sync_players(timeline_seconds)
        self.position_changed.emit(timeline_seconds)

    def _create_entry(self, clip: TimelineClip, path: Path) -> _PlayerEntry:
        output = QAudioOutput(self)
        output.setVolume(1.0)
        player = QMediaPlayer(self)
        player.setAudioOutput(output)
        player.errorOccurred.connect(
            lambda _error, message, clip_id=clip.id: self.error.emit(
                f"音声クリップ「{clip_id}」を再生できません: {message}"
            )
        )
        player.setSource(QUrl.fromLocalFile(str(path)))
        entry = _PlayerEntry(player=player, output=output, path=path)
        self._players[clip.id] = entry
        return entry

    def _sync_players(self, timeline_seconds: float) -> None:
        model = self._model_provider()
        active = active_audio_clips(model, timeline_seconds)
        active_ids = {clip.id for clip, _track in active}
        known_ids = {clip.id for clip in model.clips}

        # Drop players for clips that were deleted from the current project.
        for clip_id in tuple(self._players):
            if clip_id not in known_ids:
                entry = self._players.pop(clip_id)
                entry.player.stop()
                entry.player.setSource(QUrl())
                entry.player.deleteLater()
                entry.output.deleteLater()
                self._failed_clips.discard(clip_id)

        for clip, track in active:
            if clip.id in self._failed_clips:
                continue
            try:
                path = self._asset_resolver(clip).expanduser().resolve()
                if not path.is_file():
                    raise FileNotFoundError(path)
            except (OSError, ValueError, RuntimeError) as exc:
                self._failed_clips.add(clip.id)
                self.error.emit(f"音声素材が見つかりません: {clip.name} ({exc})")
                continue

            entry = self._players.get(clip.id)
            if entry is None or entry.path != path:
                if entry is not None:
                    entry.player.stop()
                    entry.player.deleteLater()
                    entry.output.deleteLater()
                entry = self._create_entry(clip, path)

            entry.output.setMuted(track.muted)
            source_position_ms = max(
                0, int(round(clip.source_time_at(timeline_seconds) * 1000.0))
            )
            if not entry.active:
                entry.player.setPosition(source_position_ms)
                entry.player.play()
                entry.active = True
            else:
                drift_ms = abs(entry.player.position() - source_position_ms)
                if drift_ms > self.SEEK_DRIFT_MS:
                    entry.player.setPosition(source_position_ms)

        for clip_id, entry in self._players.items():
            if clip_id not in active_ids and entry.active:
                entry.player.pause()
                entry.active = False

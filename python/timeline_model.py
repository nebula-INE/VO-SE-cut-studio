"""Non-destructive multi-track timeline model for aural Studio.

The model is intentionally independent from Qt and media decoders. Clips refer
to project asset IDs (or script line IDs); trimming changes source_in/duration
without modifying original media.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


TIMELINE_SCHEMA_VERSION = 1
TRACK_KINDS = frozenset({"video", "audio", "overlay", "motion", "other"})
CLIP_KINDS = frozenset({
    "video", "audio", "image", "voice", "text", "character", "motion", "other",
})
MIN_CLIP_DURATION_SEC = 0.05
TRACK_CLIP_KINDS = {
    "video": frozenset({"video"}),
    "audio": frozenset({"audio", "voice"}),
    "overlay": frozenset({"image", "text", "character"}),
    "motion": frozenset({"motion"}),
    "other": CLIP_KINDS,
}


class TimelineError(ValueError):
    """Raised when timeline data or an edit operation is invalid."""


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TimelineError(f"{label} must be a number") from exc
    if not math.isfinite(result):
        raise TimelineError(f"{label} must be finite")
    return result


def _validate_audio_properties(
    volume: Any, fade_in: Any, fade_out: Any, duration: Any
) -> None:
    volume_value = _number(volume, "clip volume")
    fade_in_value = _number(fade_in, "clip fade_in")
    fade_out_value = _number(fade_out, "clip fade_out")
    duration_value = _number(duration, "clip duration")
    if not 0.0 <= volume_value <= 1.0:
        raise TimelineError("Clip volume must be between 0 and 1")
    if fade_in_value < 0.0 or fade_out_value < 0.0:
        raise TimelineError("Clip fade durations cannot be negative")
    if fade_in_value > duration_value or fade_out_value > duration_value:
        raise TimelineError("Clip fade duration cannot exceed clip duration")


def _validate_character_transform(
    kind: str,
    position_x: Any,
    position_y: Any,
    scale: Any,
    rotation_deg: Any,
) -> tuple[float | None, float | None, float, float]:
    """Validate clip-level visual transforms while keeping legacy defaults."""
    if (position_x is None) != (position_y is None):
        raise TimelineError("Character position_x and position_y must both be set or both be automatic")

    normalized_x = None if position_x is None else _number(position_x, "character position_x")
    normalized_y = None if position_y is None else _number(position_y, "character position_y")
    normalized_scale = _number(scale, "character scale")
    normalized_rotation = _number(rotation_deg, "character rotation_deg")

    if normalized_x is not None and not 0.0 <= normalized_x <= 1.0:
        raise TimelineError("Character position_x must be between 0 and 1")
    if normalized_y is not None and not 0.0 <= normalized_y <= 1.0:
        raise TimelineError("Character position_y must be between 0 and 1")
    if not 0.1 <= normalized_scale <= 2.5:
        raise TimelineError("Character scale must be between 0.1 and 2.5")
    if not -180.0 <= normalized_rotation <= 180.0:
        raise TimelineError("Character rotation_deg must be between -180 and 180")
    if kind != "character" and (
        normalized_x is not None
        or normalized_y is not None
        or normalized_scale != 1.0
        or normalized_rotation != 0.0
    ):
        raise TimelineError("Visual transforms are only supported on character clips")

    return normalized_x, normalized_y, normalized_scale, normalized_rotation


@dataclass
class TimelineTrack:
    id: str
    name: str
    kind: str
    muted: bool = False
    locked: bool = False
    visible: bool = True

    @classmethod
    def create(cls, name: str, kind: str) -> "TimelineTrack":
        return cls(id=uuid.uuid4().hex, name=name.strip(), kind=kind)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimelineTrack":
        if not isinstance(data, dict):
            raise TimelineError("Timeline track must be an object")
        try:
            track = cls(
                id=str(data["id"]),
                name=str(data["name"]),
                kind=str(data["kind"]),
                muted=data.get("muted", False),
                locked=data.get("locked", False),
                visible=data.get("visible", True),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineError(f"Invalid timeline track: {exc}") from exc
        if not track.id or not track.name.strip():
            raise TimelineError("Timeline track id and name must not be empty")
        if track.kind not in TRACK_KINDS:
            raise TimelineError(f"Unsupported timeline track kind: {track.kind}")
        if not all(isinstance(flag, bool) for flag in (track.muted, track.locked, track.visible)):
            raise TimelineError("Timeline track flags must be booleans")
        return track

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TimelineClip:
    id: str
    track_id: str
    kind: str
    name: str
    start: float
    duration: float
    source_in: float = 0.0
    asset_id: str | None = None
    script_line_id: str | None = None
    enabled: bool = True
    # Qt QAudioOutput uses a normalized linear gain in the [0, 1] range.
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    # Character stage position is normalized to [0, 1]; None keeps auto layout.
    position_x: float | None = None
    position_y: float | None = None
    scale: float = 1.0
    rotation_deg: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def source_out(self) -> float:
        return self.source_in + self.duration

    def source_time_at(self, timeline_seconds: float) -> float:
        """Convert a timeline position to its clamped source-media position."""
        timeline_value = _number(timeline_seconds, "timeline time")
        offset = min(max(0.0, timeline_value - self.start), self.duration)
        return self.source_in + offset

    def timeline_time_at(self, source_seconds: float) -> float:
        """Convert a source-media timestamp to its clamped timeline position."""
        source_value = _number(source_seconds, "source time")
        offset = min(max(0.0, source_value - self.source_in), self.duration)
        return self.start + offset

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimelineClip":
        if not isinstance(data, dict):
            raise TimelineError("Timeline clip must be an object")
        try:
            clip = cls(
                id=str(data["id"]),
                track_id=str(data["track_id"]),
                kind=str(data["kind"]),
                name=str(data.get("name") or data["kind"]),
                start=_number(data["start"], "clip start"),
                duration=_number(data["duration"], "clip duration"),
                source_in=_number(data.get("source_in", 0.0), "clip source_in"),
                asset_id=str(data["asset_id"]) if data.get("asset_id") is not None else None,
                script_line_id=(
                    str(data["script_line_id"]) if data.get("script_line_id") is not None else None
                ),
                enabled=data.get("enabled", True),
                volume=_number(data.get("volume", 1.0), "clip volume"),
                fade_in=_number(data.get("fade_in", 0.0), "clip fade_in"),
                fade_out=_number(data.get("fade_out", 0.0), "clip fade_out"),
                position_x=(
                    None if data.get("position_x") is None
                    else _number(data.get("position_x"), "character position_x")
                ),
                position_y=(
                    None if data.get("position_y") is None
                    else _number(data.get("position_y"), "character position_y")
                ),
                scale=_number(data.get("scale", 1.0), "character scale"),
                rotation_deg=_number(data.get("rotation_deg", 0.0), "character rotation_deg"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, TimelineError):
                raise
            raise TimelineError(f"Invalid timeline clip: {exc}") from exc
        if not clip.id or not clip.track_id or not clip.name.strip():
            raise TimelineError("Timeline clip id, track_id and name must not be empty")
        if clip.kind not in CLIP_KINDS:
            raise TimelineError(f"Unsupported timeline clip kind: {clip.kind}")
        if clip.start < 0 or clip.source_in < 0:
            raise TimelineError("Clip start and source_in cannot be negative")
        if clip.duration < MIN_CLIP_DURATION_SEC:
            raise TimelineError(f"Clip duration must be at least {MIN_CLIP_DURATION_SEC} seconds")
        if not isinstance(clip.enabled, bool):
            raise TimelineError("Timeline clip enabled flag must be a boolean")
        if clip.asset_id is None and clip.script_line_id is None:
            raise TimelineError("Timeline clip must reference an asset or a script line")
        _validate_audio_properties(clip.volume, clip.fade_in, clip.fade_out, clip.duration)
        if clip.kind not in {"audio", "voice"} and (
            clip.volume != 1.0 or clip.fade_in != 0.0 or clip.fade_out != 0.0
        ):
            raise TimelineError("Audio properties are only supported on audio and voice clips")
        (
            clip.position_x,
            clip.position_y,
            clip.scale,
            clip.rotation_deg,
        ) = _validate_character_transform(
            clip.kind, clip.position_x, clip.position_y, clip.scale, clip.rotation_deg
        )
        return clip

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TimelineModel:
    tracks: list[TimelineTrack] = field(default_factory=list)
    clips: list[TimelineClip] = field(default_factory=list)
    frame_rate: float = 30.0

    @classmethod
    def create_default(cls, frame_rate: float = 30.0) -> "TimelineModel":
        return cls(
            frame_rate=_number(frame_rate, "timeline frame_rate"),
            tracks=[
                TimelineTrack.create("Video 1", "video"),
                TimelineTrack.create("Audio 1", "audio"),
                TimelineTrack.create("Overlay 1", "overlay"),
                TimelineTrack.create("Motion 1", "motion"),
            ],
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TimelineModel":
        if data is None or data == {}:
            return cls.create_default()
        if not isinstance(data, dict):
            raise TimelineError("Timeline root must be an object")
        version = data.get("schema_version")
        if type(version) is not int or version != TIMELINE_SCHEMA_VERSION:
            raise TimelineError(
                f"Unsupported timeline schema version: {version!r} "
                f"(supported: {TIMELINE_SCHEMA_VERSION})"
            )
        raw_tracks = data.get("tracks", [])
        raw_clips = data.get("clips", [])
        if not isinstance(raw_tracks, list) or not isinstance(raw_clips, list):
            raise TimelineError("Timeline tracks and clips must be arrays")
        model = cls(
            frame_rate=_number(data.get("frame_rate", 30.0), "timeline frame_rate"),
            tracks=[TimelineTrack.from_dict(item) for item in raw_tracks],
            clips=[TimelineClip.from_dict(item) for item in raw_clips],
        )
        if not 1.0 <= model.frame_rate <= 240.0:
            raise TimelineError("Timeline frame_rate must be between 1 and 240")
        model._validate()
        return model

    def to_dict(self) -> dict[str, Any]:
        self._validate()
        return {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "frame_rate": self.frame_rate,
            "tracks": [track.to_dict() for track in self.tracks],
            "clips": [clip.to_dict() for clip in self.clips],
        }

    def _validate(self) -> None:
        track_ids = [track.id for track in self.tracks]
        clip_ids = [clip.id for clip in self.clips]
        if len(track_ids) != len(set(track_ids)):
            raise TimelineError("Timeline contains duplicate track IDs")
        if len(clip_ids) != len(set(clip_ids)):
            raise TimelineError("Timeline contains duplicate clip IDs")
        if not 1.0 <= self.frame_rate <= 240.0 or not math.isfinite(self.frame_rate):
            raise TimelineError("Timeline frame_rate must be between 1 and 240")
        track_map = {track.id: track for track in self.tracks}
        for clip in self.clips:
            track = track_map.get(clip.track_id)
            if track is None:
                raise TimelineError(f"Clip references an unknown track: {clip.track_id}")
            if clip.kind not in TRACK_CLIP_KINDS[track.kind]:
                raise TimelineError(
                    f"Clip kind {clip.kind} is not supported on {track.kind} tracks"
                )
            if clip.start < 0 or clip.source_in < 0 or clip.duration < MIN_CLIP_DURATION_SEC:
                raise TimelineError(f"Clip has invalid timing values: {clip.id}")
            _validate_audio_properties(clip.volume, clip.fade_in, clip.fade_out, clip.duration)
            if clip.kind not in {"audio", "voice"} and (
                clip.volume != 1.0 or clip.fade_in != 0.0 or clip.fade_out != 0.0
            ):
                raise TimelineError("Audio properties are only supported on audio and voice clips")
            _validate_character_transform(
                clip.kind, clip.position_x, clip.position_y, clip.scale, clip.rotation_deg
            )

    @property
    def duration_sec(self) -> float:
        return max((clip.end for clip in self.clips), default=0.0)

    def get_track(self, track_id: str) -> TimelineTrack | None:
        return next((track for track in self.tracks if track.id == track_id), None)

    def get_clip(self, clip_id: str) -> TimelineClip | None:
        return next((clip for clip in self.clips if clip.id == clip_id), None)

    def clips_for_track(self, track_id: str) -> list[TimelineClip]:
        return sorted(
            (clip for clip in self.clips if clip.track_id == track_id),
            key=lambda clip: (clip.start, clip.id),
        )

    def next_clip_after(self, clip_id: str) -> TimelineClip | None:
        """Return the next enabled clip on the same track without crossing overlaps."""
        current = self.get_clip(clip_id)
        if current is None:
            return None
        candidates = (
            clip for clip in self.clips
            if clip.id != current.id
            and clip.track_id == current.track_id
            and clip.kind == current.kind
            and clip.enabled
            and clip.start >= current.end - 1e-6
        )
        return min(candidates, key=lambda clip: (clip.start, clip.id), default=None)

    def add_track(self, name: str, kind: str) -> TimelineTrack:
        if kind not in TRACK_KINDS:
            raise TimelineError(f"Unsupported timeline track kind: {kind}")
        if not name.strip():
            raise TimelineError("Timeline track name must not be empty")
        track = TimelineTrack.create(name, kind)
        self.tracks.append(track)
        return track

    def remove_track(self, track_id: str, *, remove_clips: bool = False) -> bool:
        track = self.get_track(track_id)
        if track is None:
            return False
        linked = [clip for clip in self.clips if clip.track_id == track_id]
        if linked and not remove_clips:
            raise TimelineError("Track still contains clips; remove clips or use remove_clips=True")
        self.clips = [clip for clip in self.clips if clip.track_id != track_id]
        self.tracks = [item for item in self.tracks if item.id != track_id]
        return True

    def add_clip(
        self,
        track_id: str,
        kind: str,
        name: str,
        duration: float,
        *,
        start: float = 0.0,
        source_in: float = 0.0,
        asset_id: str | None = None,
        script_line_id: str | None = None,
        volume: float = 1.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        position_x: float | None = None,
        position_y: float | None = None,
        scale: float = 1.0,
        rotation_deg: float = 0.0,
    ) -> TimelineClip:
        track = self.get_track(track_id)
        if track is None:
            raise TimelineError(f"Unknown timeline track: {track_id}")
        if kind not in CLIP_KINDS or kind not in TRACK_CLIP_KINDS[track.kind]:
            raise TimelineError(f"Clip kind {kind!r} is not supported on {track.kind} tracks")
        start_value = _number(start, "clip start")
        duration_value = _number(duration, "clip duration")
        source_value = _number(source_in, "clip source_in")
        if start_value < 0 or source_value < 0:
            raise TimelineError("Clip start and source_in cannot be negative")
        if duration_value < MIN_CLIP_DURATION_SEC:
            raise TimelineError(f"Clip duration must be at least {MIN_CLIP_DURATION_SEC} seconds")
        if asset_id is None and script_line_id is None:
            raise TimelineError("Timeline clip must reference an asset or a script line")
        if not name.strip():
            raise TimelineError("Timeline clip name must not be empty")
        volume_value = _number(volume, "clip volume")
        fade_in_value = _number(fade_in, "clip fade_in")
        fade_out_value = _number(fade_out, "clip fade_out")
        _validate_audio_properties(volume_value, fade_in_value, fade_out_value, duration_value)
        if kind not in {"audio", "voice"} and (
            volume_value != 1.0 or fade_in_value != 0.0 or fade_out_value != 0.0
        ):
            raise TimelineError("Audio properties are only supported on audio and voice clips")
        (
            position_x_value,
            position_y_value,
            scale_value,
            rotation_value,
        ) = _validate_character_transform(kind, position_x, position_y, scale, rotation_deg)
        clip = TimelineClip(
            id=uuid.uuid4().hex,
            track_id=track_id,
            kind=kind,
            name=name.strip(),
            start=start_value,
            duration=duration_value,
            source_in=source_value,
            asset_id=asset_id,
            script_line_id=script_line_id,
            volume=volume_value,
            fade_in=fade_in_value,
            fade_out=fade_out_value,
            position_x=position_x_value,
            position_y=position_y_value,
            scale=scale_value,
            rotation_deg=rotation_value,
        )
        self.clips.append(clip)
        return clip

    def upsert_voice_clip(
        self,
        preferred_track_id: str,
        name: str,
        duration: float,
        *,
        start: float,
        asset_id: str,
        script_line_id: str,
    ) -> tuple[TimelineClip, bool]:
        """Create a generated voice clip, but never overwrite an existing edit.

        A valid generated clip may have been moved, trimmed, or renamed by the
        user. Reopening/reconciling the script must preserve that timeline edit.
        Only its source asset reference is refreshed if the cached WAV changed.
        """
        existing = next(
            (
                clip for clip in self.clips
                if clip.kind == "voice" and clip.script_line_id == script_line_id
            ),
            None,
        )
        if existing is not None:
            changed = existing.asset_id != asset_id
            if changed:
                existing.asset_id = asset_id
            return existing, changed

        created = self.add_clip(
            preferred_track_id,
            "voice",
            name,
            duration,
            start=start,
            asset_id=asset_id,
            script_line_id=script_line_id,
        )
        return created, True

    @staticmethod
    def _snap(value: float, snap_seconds: float | None) -> float:
        if snap_seconds is None or snap_seconds <= 0:
            return value
        return round(value / snap_seconds) * snap_seconds

    def move_clip(
        self,
        clip_id: str,
        start: float,
        *,
        track_id: str | None = None,
        snap_seconds: float | None = 0.1,
    ) -> TimelineClip:
        clip = self.get_clip(clip_id)
        if clip is None:
            raise TimelineError(f"Unknown timeline clip: {clip_id}")
        new_start = self._snap(_number(start, "clip start"), snap_seconds)
        if new_start < 0:
            raise TimelineError("Clip start cannot be negative")
        destination_track = self.get_track(track_id or clip.track_id)
        if destination_track is None:
            raise TimelineError(f"Unknown timeline track: {track_id}")
        if clip.kind not in TRACK_CLIP_KINDS[destination_track.kind]:
            raise TimelineError("Clip cannot be moved to a track of a different kind")
        clip.start = new_start
        clip.track_id = destination_track.id
        return clip

    def trim_clip(
        self,
        clip_id: str,
        *,
        source_in: float | None = None,
        duration: float | None = None,
    ) -> TimelineClip:
        clip = self.get_clip(clip_id)
        if clip is None:
            raise TimelineError(f"Unknown timeline clip: {clip_id}")
        new_source_in = clip.source_in if source_in is None else _number(source_in, "clip source_in")
        new_duration = clip.duration if duration is None else _number(duration, "clip duration")
        if new_source_in < 0:
            raise TimelineError("Clip source_in cannot be negative")
        if new_duration < MIN_CLIP_DURATION_SEC:
            raise TimelineError(f"Clip duration must be at least {MIN_CLIP_DURATION_SEC} seconds")
        clip.source_in = new_source_in
        clip.duration = new_duration
        # A shorter trim must not leave an invalid fade outside the new clip bounds.
        clip.fade_in = min(clip.fade_in, new_duration)
        clip.fade_out = min(clip.fade_out, new_duration)
        return clip

    def set_audio_properties(
        self,
        clip_id: str,
        *,
        volume: float | None = None,
        fade_in: float | None = None,
        fade_out: float | None = None,
    ) -> TimelineClip:
        """Set non-destructive preview gain and edge fades for an audio clip."""
        clip = self.get_clip(clip_id)
        if clip is None:
            raise TimelineError(f"Unknown timeline clip: {clip_id}")
        track = self.get_track(clip.track_id)
        if clip.kind not in {"audio", "voice"} or track is None or track.kind != "audio":
            raise TimelineError("Audio properties require an audio or voice clip on an audio track")
        new_volume = clip.volume if volume is None else _number(volume, "clip volume")
        new_fade_in = clip.fade_in if fade_in is None else _number(fade_in, "clip fade_in")
        new_fade_out = clip.fade_out if fade_out is None else _number(fade_out, "clip fade_out")
        _validate_audio_properties(new_volume, new_fade_in, new_fade_out, clip.duration)
        clip.volume = new_volume
        clip.fade_in = new_fade_in
        clip.fade_out = new_fade_out
        return clip

    def set_character_transform(
        self,
        clip_id: str,
        *,
        position_x: float | None = None,
        position_y: float | None = None,
        scale: float = 1.0,
        rotation_deg: float = 0.0,
    ) -> TimelineClip:
        """Set normalized stage position, relative size, and rotation on a character."""
        clip = self.get_clip(clip_id)
        if clip is None:
            raise TimelineError(f"Unknown timeline clip: {clip_id}")
        if clip.kind != "character":
            raise TimelineError("Visual transforms require a character clip")
        (
            clip.position_x,
            clip.position_y,
            clip.scale,
            clip.rotation_deg,
        ) = _validate_character_transform(
            clip.kind, position_x, position_y, scale, rotation_deg
        )
        return clip

    def split_clip(self, clip_id: str, at_seconds: float) -> tuple[TimelineClip, TimelineClip]:
        clip = self.get_clip(clip_id)
        if clip is None:
            raise TimelineError(f"Unknown timeline clip: {clip_id}")
        split_at = _number(at_seconds, "split time")
        if not clip.start + MIN_CLIP_DURATION_SEC <= split_at <= clip.end - MIN_CLIP_DURATION_SEC:
            raise TimelineError("Split point must leave at least 0.05 seconds on both sides")
        left_duration = split_at - clip.start
        right = TimelineClip(
            id=uuid.uuid4().hex,
            track_id=clip.track_id,
            kind=clip.kind,
            name=f"{clip.name} (2)",
            start=split_at,
            duration=clip.duration - left_duration,
            source_in=clip.source_in + left_duration,
            asset_id=clip.asset_id,
            script_line_id=clip.script_line_id,
            enabled=clip.enabled,
            volume=clip.volume,
            fade_in=0.0,
            fade_out=min(clip.fade_out, clip.duration - left_duration),
            position_x=clip.position_x,
            position_y=clip.position_y,
            scale=clip.scale,
            rotation_deg=clip.rotation_deg,
        )
        clip.duration = left_duration
        clip.fade_in = min(clip.fade_in, left_duration)
        clip.fade_out = 0.0
        self.clips.append(right)
        return clip, right

    def remove_clip(self, clip_id: str) -> bool:
        if self.get_clip(clip_id) is None:
            return False
        self.clips = [clip for clip in self.clips if clip.id != clip_id]
        return True

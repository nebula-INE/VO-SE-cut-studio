"""Resolve a live preview state from the timeline's active motion clip."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from character_motion import sample_motion
from timeline_model import TimelineClip, TimelineModel


class TimelineMotionPreviewError(ValueError):
    """Raised when the active timeline motion cannot be previewed safely."""


def timeline_motion_state_at(
    model: TimelineModel,
    timeline_seconds: float,
    load_keyframes: Callable[[TimelineClip], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Return the interpolated motion state at a timeline time, if one is active.

    Hidden, muted, disabled, and out-of-range clips never drive the preview.
    Motion is applied only while a visible character clip is also active.
    """
    try:
        position = float(timeline_seconds)
    except (TypeError, ValueError) as exc:
        raise TimelineMotionPreviewError("Timeline position must be finite") from exc
    if not math.isfinite(position) or position < 0.0:
        raise TimelineMotionPreviewError("Timeline position must be finite and non-negative")

    tracks = {track.id: track for track in model.tracks}
    active_characters = [
        clip
        for clip in model.clips
        if clip.kind == "character"
        and clip.enabled
        and clip.start <= position < clip.end
        and (track := tracks.get(clip.track_id)) is not None
        and track.visible
        and not track.muted
    ]

    active_motion: list[TimelineClip] = []
    for track in model.tracks:
        if track.kind != "motion" or not track.visible or track.muted:
            continue
        active_motion.extend(
            clip
            for clip in model.clips_for_track(track.id)
            if clip.kind == "motion"
            and clip.enabled
            and clip.start <= position < clip.end
        )

    if not active_motion or not active_characters:
        return None
    if len(active_motion) > 1:
        raise TimelineMotionPreviewError(
            "同じ時間帯に複数のモーションクリップが重なっています。"
        )

    clip = active_motion[0]
    if clip.asset_id is None:
        raise TimelineMotionPreviewError(
            f"モーションクリップ「{clip.name}」にモーションJSON素材がありません"
        )

    keyframes = load_keyframes(clip)
    source_seconds = clip.source_in + position - clip.start
    return sample_motion(keyframes, source_seconds)

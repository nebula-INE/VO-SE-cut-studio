"""Synchronize generated subtitle clips with valid script audio without overwriting edits."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from timeline_model import MIN_CLIP_DURATION_SEC, TimelineModel


def sync_generated_subtitle_clips(
    model: TimelineModel,
    script_lines: Iterable[Mapping[str, Any]],
    valid_audio_line_ids: set[str],
) -> bool:
    """Create one subtitle clip per synthesized script line and prune stale generated clips.

    Subtitle timing follows the paired Voice clip. Auto-generated captions update
    when that audio clip is regenerated or edited; once the caption itself is
    manually moved or trimmed, its timing is preserved. Text is resolved from the
    current script at export time.
    """
    valid_ids = set(valid_audio_line_ids)
    lines = list(script_lines)
    valid_ids = {
        str(line.get("line_id", ""))
        for line in lines
        if str(line.get("line_id", "")) in valid_ids
        and str(line.get("text", "")).strip()
    }
    voice_clips = {
        clip.script_line_id: clip
        for clip in model.clips
        if clip.kind == "voice" and clip.script_line_id in valid_ids
    }
    valid_ids.intersection_update(voice_clips)

    original_count = len(model.clips)
    model.clips = [
        clip for clip in model.clips
        if not (
            clip.kind == "text"
            and clip.script_line_id is not None
            and clip.script_line_id not in valid_ids
        )
    ]
    changed = len(model.clips) != original_count

    eligible = [
        line for line in lines
        if str(line.get("line_id", "")) in valid_ids
    ]
    if not eligible:
        return changed

    existing_clips = {
        clip.script_line_id: clip
        for clip in model.clips
        if clip.kind == "text" and clip.script_line_id in valid_ids
    }
    missing_ids = valid_ids - set(existing_clips)
    subtitle_track = next(
        (
            track for track in model.tracks
            if track.kind == "overlay"
            and track.name.lower() in {"subtitles", "字幕"}
            and not track.locked
        ),
        None,
    )
    if subtitle_track is None and not missing_ids:
        existing = next(iter(existing_clips.values()), None)
        subtitle_track = model.get_track(existing.track_id) if existing is not None else None
    if subtitle_track is None:
        base_name = "Subtitles"
        existing_names = {track.name.lower() for track in model.tracks}
        name = base_name
        suffix = 2
        while name.lower() in existing_names:
            name = f"{base_name} {suffix}"
            suffix += 1
        subtitle_track = model.add_track(name, "overlay")
        changed = True

    for line in lines:
        line_id = str(line.get("line_id", ""))
        voice_clip = voice_clips.get(line_id)
        if line_id not in valid_ids or voice_clip is None:
            continue
        speaker = str(line.get("speaker_name", "")).strip()
        text = str(line.get("text", "")).strip()
        label = f"{speaker}: {text[:28]}" if speaker else text[:36]
        _clip, created = model.upsert_script_subtitle_clip(
            subtitle_track.id,
            label or "Subtitle",
            voice_clip.duration,
            start=voice_clip.start,
            script_line_id=line_id,
        )
        changed = changed or created

    return changed

from script_timeline_sync import sync_generated_subtitle_clips
from timeline_model import TimelineModel


def _lines():
    return [
        {"line_id": "line-a", "speaker_name": "Narrator", "text": "First line", "duration_sec": 1.25},
        {"line_id": "line-b", "speaker_name": "Actor B", "text": "Second line", "duration_sec": 2.0},
        {"line_id": "line-c", "speaker_name": "Narrator", "text": "Not ready yet", "duration_sec": 0.75},
    ]


def _add_voice_clips(model, lines, valid_ids):
    track = next(track for track in model.tracks if track.kind == "audio")
    timeline_start = 0.0
    clips = {}
    for line in lines:
        line_id = line["line_id"]
        if line_id in valid_ids:
            clip, _changed = model.upsert_voice_clip(
                track.id,
                f'{line["speaker_name"]}: {line["text"][:28]}',
                line["duration_sec"],
                start=timeline_start,
                asset_id=f"asset-{line_id}",
                script_line_id=line_id,
            )
            clips[line_id] = clip
        timeline_start += line["duration_sec"]
    return clips


def test_sync_creates_subtitle_clips_at_paired_voice_positions():
    model = TimelineModel.create_default()
    lines = _lines()
    _add_voice_clips(model, lines, {"line-a", "line-b"})

    changed = sync_generated_subtitle_clips(model, lines, {"line-a", "line-b"})

    assert changed
    track = next(track for track in model.tracks if track.name == "Subtitles")
    clips = sorted(
        (clip for clip in model.clips if clip.kind == "text"),
        key=lambda clip: clip.start,
    )
    assert len(clips) == 2
    assert clips[0].track_id == track.id
    assert clips[0].script_line_id == "line-a"
    assert clips[0].name == "Narrator: First line"
    assert clips[0].start == 0.0
    assert clips[0].duration == 1.25
    assert clips[1].script_line_id == "line-b"
    assert clips[1].start == 1.25
    assert clips[1].duration == 2.0


def test_caption_timing_tracks_voice_clip_moves_and_trims():
    model = TimelineModel.create_default()
    lines = _lines()
    voices = _add_voice_clips(model, lines, {"line-a", "line-b"})
    sync_generated_subtitle_clips(model, lines, {"line-a", "line-b"})
    caption = next(clip for clip in model.clips if clip.script_line_id == "line-a")
    alternate = model.add_track("Voice edit", "audio")
    model.move_clip(voices["line-a"].id, 8.4, track_id=alternate.id, snap_seconds=None)
    model.trim_clip(voices["line-a"].id, duration=0.6)

    changed = sync_generated_subtitle_clips(model, lines, {"line-a", "line-b"})

    assert changed
    assert caption.start == 8.4
    assert caption.duration == 0.6


def test_sync_preserves_manually_moved_and_trimmed_subtitle_clips():
    model = TimelineModel.create_default()
    lines = _lines()
    _add_voice_clips(model, lines, {"line-a", "line-b"})
    sync_generated_subtitle_clips(model, lines, {"line-a", "line-b"})
    existing = next(clip for clip in model.clips if clip.script_line_id == "line-a")
    original_track_id = existing.track_id
    model.move_clip(existing.id, 8.4, snap_seconds=None)
    model.trim_clip(existing.id, duration=0.6)

    changed = sync_generated_subtitle_clips(
        model,
        [
            {"line_id": "line-a", "speaker_name": "Narrator", "text": "Updated text", "duration_sec": 4.0},
            {"line_id": "line-b", "speaker_name": "Actor B", "text": "Second line", "duration_sec": 2.0},
        ],
        {"line-a", "line-b"},
    )

    restored = model.get_clip(existing.id)
    assert not changed
    assert restored is existing
    assert restored.track_id == original_track_id
    assert restored.start == 8.4
    assert restored.duration == 0.6
    assert restored.name == "Narrator: First line"


def test_sync_removes_stale_script_linked_subtitles_but_keeps_manual_text_clips():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    generated = model.add_clip(
        overlay.id, "text", "Generated", 1.0, script_line_id="removed-line"
    )
    manual = model.add_clip(
        overlay.id, "text", "My manual title", 2.0, asset_id="manual-title"
    )

    changed = sync_generated_subtitle_clips(model, _lines(), set())

    assert changed
    assert model.get_clip(generated.id) is None
    assert model.get_clip(manual.id) is manual
    assert not any(clip.kind == "text" and clip.script_line_id for clip in model.clips)


def test_sync_is_idempotent_when_script_and_generated_audio_are_unchanged():
    model = TimelineModel.create_default()
    lines = _lines()
    _add_voice_clips(model, lines, {"line-a", "line-b"})

    assert sync_generated_subtitle_clips(model, lines, {"line-a", "line-b"})
    assert not sync_generated_subtitle_clips(model, lines, {"line-a", "line-b"})
    assert len([clip for clip in model.clips if clip.kind == "text"]) == 2


def test_sync_updates_unedited_generated_caption_timing_after_regeneration():
    model = TimelineModel.create_default()
    initial = [
        {"line_id": "line-a", "speaker_name": "Narrator", "text": "First", "duration_sec": 1.0},
        {"line_id": "line-b", "speaker_name": "Actor", "text": "Second", "duration_sec": 2.0},
    ]
    voices = _add_voice_clips(model, initial, {"line-a", "line-b"})
    sync_generated_subtitle_clips(model, initial, {"line-a", "line-b"})
    first = next(clip for clip in model.clips if clip.script_line_id == "line-a")
    second = next(clip for clip in model.clips if clip.script_line_id == "line-b")
    assert first.auto_generated
    assert second.auto_generated

    regenerated = [
        {"line_id": "line-a", "speaker_name": "Narrator", "text": "Updated", "duration_sec": 1.5},
        {"line_id": "line-b", "speaker_name": "Actor", "text": "Second", "duration_sec": 0.8},
    ]
    voice_track = next(track for track in model.tracks if track.kind == "audio")
    timeline_start = 0.0
    for line in regenerated:
        voice, _changed = model.upsert_voice_clip(
            voice_track.id,
            f'{line["speaker_name"]}: {line["text"]}',
            line["duration_sec"],
            start=timeline_start,
            asset_id=f'asset-{line["line_id"]}-v2',
            script_line_id=line["line_id"],
        )
        timeline_start += line["duration_sec"]

    changed = sync_generated_subtitle_clips(model, regenerated, {"line-a", "line-b"})

    assert changed
    assert first.name == "Narrator: Updated"
    assert first.duration == 1.5
    assert second.start == 1.5
    assert second.duration == 0.8


def test_generated_flag_round_trips_and_legacy_clips_default_to_manual():
    model = TimelineModel.create_default()
    overlay = next(track for track in model.tracks if track.kind == "overlay")
    created, _changed = model.upsert_script_subtitle_clip(
        overlay.id, "Caption", 1.0, start=0.0, script_line_id="line-a"
    )

    restored = TimelineModel.from_dict(model.to_dict()).get_clip(created.id)
    assert restored is not None
    assert restored.auto_generated is True

    payload = model.to_dict()
    payload["clips"][0].pop("auto_generated")
    legacy = TimelineModel.from_dict(payload).get_clip(created.id)
    assert legacy is not None
    assert legacy.auto_generated is False

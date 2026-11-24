from __future__ import annotations

from timeline_audio import active_audio_clips
from timeline_model import TimelineModel


def test_audio_schedule_returns_concurrent_enabled_clips():
    model = TimelineModel.create_default()
    audio_track = next(track for track in model.tracks if track.kind == "audio")
    second_track = model.add_track("Audio 2", "audio")
    first = model.add_clip(
        audio_track.id, "audio", "music.wav", 8.0,
        start=1.0, source_in=2.0, asset_id="music",
    )
    second = model.add_clip(
        second_track.id, "audio", "voice.wav", 3.0,
        start=4.0, source_in=0.5, asset_id="voice",
    )

    active = active_audio_clips(model, 4.5)

    assert [clip.id for clip, _track in active] == [first.id, second.id]
    assert first.source_time_at(4.5) == 5.5
    assert second.source_time_at(4.5) == 1.0


def test_audio_schedule_respects_clip_boundaries_and_track_mute():
    model = TimelineModel.create_default()
    track = next(track for track in model.tracks if track.kind == "audio")
    clip = model.add_clip(
        track.id, "audio", "music.wav", 2.0,
        start=3.0, asset_id="music",
    )

    assert active_audio_clips(model, 2.99) == []
    assert active_audio_clips(model, 3.0) == [(clip, track)]
    assert active_audio_clips(model, 5.0) == []

    track.muted = True
    assert active_audio_clips(model, 4.0) == []


def test_audio_schedule_ignores_disabled_and_non_audio_clips():
    model = TimelineModel.create_default()
    audio_track = next(track for track in model.tracks if track.kind == "audio")
    video_track = next(track for track in model.tracks if track.kind == "video")
    disabled = model.add_clip(
        audio_track.id, "audio", "disabled.wav", 5.0, asset_id="disabled"
    )
    disabled.enabled = False
    model.add_clip(
        video_track.id, "video", "video.mp4", 5.0, asset_id="video"
    )

    assert active_audio_clips(model, 1.0) == []


def test_voice_clips_are_scheduled_as_audio():
    model = TimelineModel.create_default()
    audio_track = next(track for track in model.tracks if track.kind == "audio")
    voice = model.add_clip(
        audio_track.id, "voice", "narration.wav", 1.5, asset_id="voice"
    )

    assert active_audio_clips(model, 0.5) == [(voice, audio_track)]

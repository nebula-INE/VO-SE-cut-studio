from __future__ import annotations

import json
import os
import shutil
import time
import wave
from pathlib import Path

import pytest

from project_model import ProjectDocument, ProjectError, PROJECT_FORMAT, PROJECT_SCHEMA_VERSION
from timeline_model import TimelineModel


def test_project_round_trip_preserves_script_and_settings(tmp_path):
    path = tmp_path / "sample.auralproj"
    project = ProjectDocument.create("Sample")
    project.script = {
        "speakers": [{"name": "Narrator", "color": "#123456", "voice_source": "openjtalk"}],
        "lines": [{"speaker_name": "Narrator", "text": "Hello", "line_id": "line-1"}],
    }
    project.settings = {"frame_rate": 30, "resolution": [1920, 1080]}

    saved = project.save(path)
    loaded = ProjectDocument.load(saved)

    assert loaded.name == "Sample"
    assert loaded.script == project.script
    assert loaded.settings == project.settings
    assert loaded.project_id == project.project_id
    assert loaded.file_path == saved.resolve()


def test_managed_asset_is_relative_and_survives_project_relocation(tmp_path):
    original_dir = tmp_path / "original"
    original_dir.mkdir()
    project = ProjectDocument.create("Portable")
    project.save(original_dir / "portable.auralproj")

    source = tmp_path / "footage.mov"
    source.write_bytes(b"video-bytes")
    asset = project.add_asset(source, "video", copy_into_project=True)

    assert not Path(asset.path).is_absolute()
    assert asset.managed
    assert project.resolve_asset(asset).read_bytes() == b"video-bytes"
    project.save()

    relocated_dir = tmp_path / "relocated"
    shutil.copytree(original_dir, relocated_dir)
    relocated = ProjectDocument.load(relocated_dir / "portable.auralproj")
    relocated_asset = relocated.get_asset(asset.id)
    assert relocated_asset is not None
    assert relocated.resolve_asset(relocated_asset).read_bytes() == b"video-bytes"


def test_linked_asset_keeps_external_absolute_path(tmp_path):
    source = tmp_path / "music.wav"
    source.write_bytes(b"audio")
    project = ProjectDocument.create("Linked")
    project.save(tmp_path / "linked.auralproj")

    asset = project.add_asset(source, "audio")
    assert not asset.managed
    assert Path(asset.path).is_absolute()
    assert project.resolve_asset(asset) == source.resolve()


def test_duplicate_asset_registration_returns_existing_entry(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"clip")
    project = ProjectDocument.create()
    project.save(tmp_path / "dedupe.auralproj")

    first = project.add_asset(source, "video")
    second = project.add_asset(source, "video")
    assert first.id == second.id
    assert len(project.assets) == 1


def test_relative_path_traversal_is_rejected(tmp_path):
    project = ProjectDocument.create()
    project.save(tmp_path / "safe.auralproj")
    payload = project.to_dict()
    payload["assets"] = [{
        "id": "bad", "name": "bad", "kind": "video",
        "path": "../outside.mp4", "managed": True,
    }]
    loaded = ProjectDocument.from_dict(payload, tmp_path / "safe.auralproj")
    with pytest.raises(ProjectError, match="traversal"):
        loaded.resolve_asset("bad")


def test_managed_asset_cannot_use_absolute_path(tmp_path):
    project = ProjectDocument.create()
    project.save(tmp_path / "safe.auralproj")
    payload = project.to_dict()
    payload["assets"] = [{
        "id": "bad", "name": "bad", "kind": "video",
        "path": str(tmp_path / "outside.mp4"), "managed": True,
    }]
    loaded = ProjectDocument.from_dict(payload, tmp_path / "safe.auralproj")
    with pytest.raises(ProjectError, match="relative"):
        loaded.resolve_asset("bad")


def test_legacy_script_json_can_be_opened_as_project(tmp_path):
    path = tmp_path / "old-script.json"
    data = {
        "speakers": [{"name": "A", "color": "#ffffff"}],
        "lines": [{"speaker_name": "A", "text": "Legacy", "line_id": "old-1"}],
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    project = ProjectDocument.load(path)
    assert project.name == "old-script"
    assert project.script == data
    assert project.file_path == path.resolve()
    migrated = project.save()
    assert migrated.suffix == ".auralproj"
    assert ProjectDocument.load(migrated).script == data


def test_newer_autosave_is_detected_and_loaded(tmp_path):
    path = tmp_path / "recover.auralproj"
    project = ProjectDocument.create("Before")
    project.save(path)
    project.name = "Recovered"
    autosave = project.save_autosave()
    future = time.time() + 2
    os.utime(autosave, (future, future))

    loaded, recovered = ProjectDocument.load_with_recovery(path)
    assert recovered
    assert loaded.name == "Recovered"
    assert loaded.file_path == path.resolve()


def test_save_clears_autosave_and_schema_is_explicit(tmp_path):
    project = ProjectDocument.create("Clean")
    path = project.save(tmp_path / "clean.auralproj")
    autosave = project.save_autosave()
    assert autosave.exists()

    project.name = "Saved"
    project.save()
    assert not autosave.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["project_format"] == PROJECT_FORMAT
    assert payload["schema_version"] == PROJECT_SCHEMA_VERSION


def test_unknown_schema_version_is_rejected():
    with pytest.raises(ProjectError, match="schema version"):
        ProjectDocument.from_dict({
            "project_format": PROJECT_FORMAT,
            "schema_version": PROJECT_SCHEMA_VERSION + 1,
        })

def test_save_as_copies_managed_assets_to_new_project_root(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio-data")
    project = ProjectDocument.create("Move")
    project.save(first_dir / "move.auralproj")
    asset = project.add_asset(source, "audio", copy_into_project=True)
    project.save()

    new_path = project.save(second_dir / "renamed.auralproj")
    assert new_path == second_dir / "renamed.auralproj"
    assert project.resolve_asset(asset).read_bytes() == b"audio-data"
    assert (first_dir / asset.path).read_bytes() == b"audio-data"
    reloaded = ProjectDocument.load(new_path)
    assert reloaded.resolve_asset(asset).read_bytes() == b"audio-data"


def test_windows_style_relative_traversal_is_rejected_on_all_hosts(tmp_path):
    project = ProjectDocument.create()
    project.save(tmp_path / "safe.auralproj")
    payload = project.to_dict()
    payload["assets"] = [{
        "id": "bad", "name": "bad", "kind": "video",
        "path": r"..\\..\\outside.mp4", "managed": True,
    }]
    loaded = ProjectDocument.from_dict(payload, tmp_path / "safe.auralproj")
    with pytest.raises(ProjectError, match="traversal"):
        loaded.resolve_asset("bad")

def test_corrupt_newer_autosave_does_not_block_opening_saved_project(tmp_path):
    path = tmp_path / "stable.auralproj"
    project = ProjectDocument.create("Stable")
    project.save(path)
    autosave = ProjectDocument.autosave_path_for(path)
    autosave.write_text("{corrupt", encoding="utf-8")
    future = time.time() + 2
    os.utime(autosave, (future, future))

    loaded, recovered = ProjectDocument.load_with_recovery(path)
    assert not recovered
    assert loaded.name == "Stable"

def test_linked_asset_can_be_promoted_to_managed_copy(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"portable-video")
    project = ProjectDocument.create("Promote")
    project.save(tmp_path / "promote.auralproj")

    linked = project.add_asset(source, "video", copy_into_project=False)
    assert not linked.managed
    assert Path(linked.path).is_absolute()

    managed = project.add_asset(source, "video", copy_into_project=True)
    assert managed.id == linked.id
    assert managed.managed
    assert not Path(managed.path).is_absolute()
    assert project.resolve_asset(managed).read_bytes() == b"portable-video"
    assert len(project.assets) == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require elevated privileges on Windows")
def test_managed_asset_copy_rejects_assets_symlink_outside_project(tmp_path):
    project_root = tmp_path / "project"
    outside_root = tmp_path / "outside"
    project_root.mkdir()
    outside_root.mkdir()
    project = ProjectDocument.create("Symlink Safety")
    project.save(project_root / "safe.auralproj")

    source = tmp_path / "payload.bin"
    source.write_bytes(b"do-not-write-outside")
    (project_root / "assets").symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ProjectError, match="escapes the project folder"):
        project.add_asset(source, "other", copy_into_project=True)
    assert list(outside_root.iterdir()) == []


@pytest.mark.parametrize("managed_path", ["safe.auralproj", "keep.txt", "../outside.txt"])
def test_managed_asset_cannot_delete_project_or_arbitrary_file(tmp_path, managed_path):
    project_path = tmp_path / "safe.auralproj"
    project = ProjectDocument.create("Safe")
    project.save(project_path)
    keep = tmp_path / "keep.txt"
    keep.write_text("keep me", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    payload = project.to_dict()
    payload["assets"] = [{
        "id": "forged", "name": "forged", "kind": "other",
        "path": managed_path, "managed": True,
    }]
    loaded = ProjectDocument.from_dict(payload, project_path)

    with pytest.raises(ProjectError, match="traversal|assets/<kind>"):
        loaded.remove_asset("forged", delete_managed_file=True)
    assert project_path.is_file()
    assert keep.read_text(encoding="utf-8") == "keep me"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_managed_asset_removal_deletes_only_file_inside_assets_kind(tmp_path):
    project = ProjectDocument.create("Delete Asset")
    project_path = project.save(tmp_path / "delete.auralproj")
    source = tmp_path / "music.wav"
    source.write_bytes(b"managed-audio")
    asset = project.add_asset(source, "audio", copy_into_project=True)
    project.save()

    resolved = project.resolve_asset(asset)
    assert resolved.is_file()
    assert project.remove_asset(asset.id, delete_managed_file=True)
    assert not resolved.exists()
    assert project_path.is_file()
    assert project.get_asset(asset.id) is None


@pytest.mark.parametrize("managed", [False, True])
def test_asset_path_with_control_characters_is_rejected(tmp_path, managed):
    project = ProjectDocument.create("Invalid Path")
    project.save(tmp_path / "invalid-path.auralproj")
    path = "assets/audio/bad\x00.wav" if managed else str(tmp_path / "bad\x00.wav")
    payload = project.to_dict()
    payload["assets"] = [{
        "id": "bad", "name": "bad", "kind": "audio",
        "path": path, "managed": managed,
    }]

    with pytest.raises(ProjectError, match="control characters"):
        ProjectDocument.from_dict(payload, tmp_path / "invalid-path.auralproj")


def test_asset_referenced_by_timeline_clip_cannot_be_removed(tmp_path):
    project = ProjectDocument.create("Referenced Asset")
    project.save(tmp_path / "referenced.auralproj")
    source = tmp_path / "voice.wav"
    source.write_bytes(b"voice-data")
    asset = project.add_asset(source, "audio")
    project.timeline["clips"] = [{
        "id": "clip-1",
        "asset_id": asset.id,
        "kind": "audio",
    }]

    with pytest.raises(ProjectError, match="referenced by a timeline clip"):
        project.remove_asset(asset.id, delete_managed_file=True)

    assert project.get_asset(asset.id) is asset
    assert source.is_file()


def test_generated_audio_asset_mapping_survives_project_reload_and_save_as(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "relocated"
    first_root.mkdir()
    second_root.mkdir()

    generated = tmp_path / "generated.wav"
    with wave.open(str(generated), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(bytes(240))

    original_wav_bytes = generated.read_bytes()
    project = ProjectDocument.create("Generated Audio")
    project.save(first_root / "generated.auralproj")
    asset = project.add_asset(generated, "audio", copy_into_project=True)
    timeline = TimelineModel.create_default()
    audio_track = next(track for track in timeline.tracks if track.kind == "audio")
    clip = timeline.add_clip(
        audio_track.id,
        "voice",
        "Narrator: persisted line",
        1.25,
        start=0.0,
        asset_id=asset.id,
        script_line_id="line-001",
    )
    project.timeline = timeline.to_dict()
    project.settings["generated_audio_assets"] = {
        "line-001": {
            "asset_id": asset.id,
            "fingerprint": "sha256:line-content-and-voice",
        }
    }
    project.save()

    loaded = ProjectDocument.load(first_root / "generated.auralproj")
    record = loaded.settings["generated_audio_assets"]["line-001"]
    restored_asset = loaded.get_asset(record["asset_id"])
    assert restored_asset is not None
    assert restored_asset.managed
    assert loaded.resolve_asset(restored_asset).read_bytes() == original_wav_bytes
    restored_timeline = TimelineModel.from_dict(loaded.timeline)
    restored_clip = restored_timeline.get_clip(clip.id)
    assert restored_clip is not None
    assert restored_clip.kind == "voice"
    assert restored_clip.script_line_id == "line-001"
    assert restored_clip.asset_id == restored_asset.id

    relocated_path = loaded.save(second_root / "renamed.auralproj")
    relocated = ProjectDocument.load(relocated_path)
    relocated_record = relocated.settings["generated_audio_assets"]["line-001"]
    relocated_asset = relocated.get_asset(relocated_record["asset_id"])
    assert relocated_asset is not None
    assert relocated.resolve_asset(relocated_asset).read_bytes() == original_wav_bytes
    assert relocated_record["fingerprint"] == "sha256:line-content-and-voice"

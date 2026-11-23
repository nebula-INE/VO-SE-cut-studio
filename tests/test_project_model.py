from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from project_model import ProjectDocument, ProjectError, PROJECT_FORMAT, PROJECT_SCHEMA_VERSION


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

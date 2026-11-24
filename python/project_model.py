"""Project document and asset management for aural Studio.

The project file is a UTF-8 JSON document with the `.auralproj` suffix.
Asset references are either:
- managed: copied below the project folder and stored as relative paths;
- linked: left in place and stored as absolute paths.

The module intentionally has no Qt or native-engine dependency so that project
files can be validated and tested on every platform.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


PROJECT_FORMAT = "aural-studio-project"
PROJECT_SCHEMA_VERSION = 1
PROJECT_SUFFIX = ".auralproj"
ASSET_KINDS = frozenset({"video", "audio", "image", "character", "voicebank", "other"})


class ProjectError(ValueError):
    """Raised when a project document or one of its asset references is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_script() -> dict[str, Any]:
    return {"speakers": [], "lines": []}


def _safe_asset_name(name: str) -> str:
    """Keep the original extension while removing path/control characters."""
    base = Path(name).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return base or "asset"


@dataclass
class ProjectAsset:
    id: str
    name: str
    kind: str
    path: str
    managed: bool = False
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectAsset":
        if not isinstance(data, dict):
            raise ProjectError("Asset entry must be an object")
        try:
            asset = cls(
                id=str(data["id"]),
                name=str(data["name"]),
                kind=str(data["kind"]),
                path=str(data["path"]),
                managed=bool(data.get("managed", False)),
                size_bytes=data.get("size_bytes"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectError(f"Invalid asset entry: {exc}") from exc
        if not asset.id or not asset.name or not asset.path:
            raise ProjectError("Asset id, name and path must not be empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in asset.path):
            raise ProjectError("Asset path must not contain control characters")
        if asset.kind not in ASSET_KINDS:
            raise ProjectError(f"Unsupported asset kind: {asset.kind}")
        if not isinstance(data.get("managed", False), bool):
            raise ProjectError("Asset managed flag must be a boolean")
        if asset.size_bytes is not None:
            if isinstance(asset.size_bytes, bool):
                raise ProjectError("Asset size_bytes must be an integer or null")
            try:
                asset.size_bytes = int(asset.size_bytes)
            except (TypeError, ValueError) as exc:
                raise ProjectError("Asset size_bytes must be an integer or null") from exc
            if asset.size_bytes < 0:
                raise ProjectError("Asset size_bytes cannot be negative")
        return asset


@dataclass
class ProjectDocument:
    """Serializable project state, independent from the GUI.

    `file_path` is runtime metadata and is never embedded in the document.
    The script payload deliberately matches ScriptModel.to_dict(), preserving
    compatibility with the existing standalone script JSON format.
    """

    name: str = "Untitled"
    script: dict[str, Any] = field(default_factory=_default_script)
    assets: list[ProjectAsset] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    project_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    file_path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def create(cls, name: str = "Untitled") -> "ProjectDocument":
        clean_name = str(name).strip() or "Untitled"
        return cls(name=clean_name)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], file_path: str | Path | None = None
    ) -> "ProjectDocument":
        if not isinstance(data, dict):
            raise ProjectError("Project root must be a JSON object")

        # Older standalone script JSON has no project format marker. Treat it as
        # a project with the script data intact so users can migrate by saving it.
        if "project_format" not in data and "schema_version" not in data:
            if not isinstance(data.get("speakers", []), list) or not isinstance(data.get("lines", []), list):
                raise ProjectError("Legacy script JSON has an invalid structure")
            document = cls.create(Path(file_path).stem if file_path else "Imported Script")
            document.script = {
                "speakers": data.get("speakers", []),
                "lines": data.get("lines", []),
            }
            document.file_path = Path(file_path).resolve() if file_path else None
            return document

        if data.get("project_format") != PROJECT_FORMAT:
            raise ProjectError("This file is not an aural Studio project")
        version = data.get("schema_version")
        if type(version) is not int or version != PROJECT_SCHEMA_VERSION:
            raise ProjectError(
                f"Unsupported project schema version: {version!r} "
                f"(supported: {PROJECT_SCHEMA_VERSION})"
            )

        script = data.get("script", _default_script())
        if not isinstance(script, dict):
            raise ProjectError("Project script must be an object")
        if not isinstance(script.get("speakers", []), list) or not isinstance(script.get("lines", []), list):
            raise ProjectError("Project script speakers and lines must be arrays")

        settings = data.get("settings", {})
        if not isinstance(settings, dict):
            raise ProjectError("Project settings must be an object")
        raw_assets = data.get("assets", [])
        if not isinstance(raw_assets, list):
            raise ProjectError("Project assets must be an array")

        document = cls(
            name=str(data.get("name") or "Untitled"),
            script=script,
            assets=[ProjectAsset.from_dict(item) for item in raw_assets],
            settings=settings,
            project_id=str(data.get("project_id") or uuid.uuid4().hex),
            created_at=str(data.get("created_at") or _utc_now()),
            updated_at=str(data.get("updated_at") or _utc_now()),
            file_path=Path(file_path).resolve() if file_path else None,
        )
        document._validate_unique_asset_ids()
        return document

    @classmethod
    def load(cls, path: str | Path) -> "ProjectDocument":
        source = Path(path).expanduser()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectError(f"Could not read project file {source}: {exc}") from exc
        return cls.from_dict(payload, source)

    @classmethod
    def load_with_recovery(cls, path: str | Path) -> tuple["ProjectDocument", bool]:
        """Load a newer autosave when present. The caller decides whether to ask."""
        source = Path(path).expanduser().resolve()
        document = cls.load(source)
        autosave = cls.autosave_path_for(source)
        if not autosave.is_file():
            return document, False
        try:
            use_recovery = autosave.stat().st_mtime_ns > source.stat().st_mtime_ns
        except OSError:
            return document, False
        if not use_recovery:
            return document, False
        try:
            recovered = cls.load(autosave)
        except (OSError, ProjectError):
            # A truncated autosave must not make the last manually saved project
            # impossible to open.
            return document, False
        recovered.file_path = source
        return recovered, True

    def to_dict(self) -> dict[str, Any]:
        self._validate_unique_asset_ids()
        return {
            "project_format": PROJECT_FORMAT,
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "script": self.script,
            "assets": [asset.to_dict() for asset in self.assets],
            "settings": self.settings,
        }

    def _validate_unique_asset_ids(self) -> None:
        ids = [asset.id for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise ProjectError("Project contains duplicate asset IDs")

    @staticmethod
    def autosave_path_for(project_path: str | Path) -> Path:
        path = Path(project_path)
        return path.with_name(path.name + ".autosave")

    @property
    def autosave_path(self) -> Path | None:
        return self.autosave_path_for(self.file_path) if self.file_path else None

    def save(self, path: str | Path | None = None) -> Path:
        destination = Path(path).expanduser() if path is not None else self.file_path
        if destination is None:
            raise ProjectError("Project has no file path; choose a destination first")
        if destination.suffix.lower() != PROJECT_SUFFIX:
            destination = destination.with_suffix(PROJECT_SUFFIX)
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        previous_path = self.file_path
        copied_assets: list[Path] = []
        if previous_path is not None and previous_path.parent != destination.parent:
            copied_assets = self._copy_managed_assets_to(destination.parent)

        old_autosave = self.autosave_path_for(previous_path) if previous_path else None
        try:
            self.updated_at = _utc_now()
            self._write_atomic(destination)
        except Exception:
            for copied in reversed(copied_assets):
                try:
                    if copied.is_dir():
                        shutil.rmtree(copied)
                    else:
                        copied.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        self.file_path = destination
        new_autosave = self.autosave_path
        for autosave in (old_autosave, new_autosave):
            if autosave and autosave.exists():
                try:
                    autosave.unlink()
                except OSError:
                    pass
        return destination

    def _copy_managed_assets_to(self, new_root: Path) -> list[Path]:
        """Copy project-owned assets when Save As changes the project directory."""
        if self.file_path is None:
            return []
        target_root = new_root.resolve()
        copied: list[Path] = []
        try:
            for asset in self.assets:
                if not asset.managed:
                    continue
                source = self.resolve_asset(asset)
                if not source.exists():
                    continue
                relative = Path(asset.path)
                destination = (target_root / relative).resolve()
                try:
                    destination.relative_to(target_root)
                except ValueError as exc:
                    raise ProjectError(
                        f"Managed asset path escapes the project folder: {asset.path}"
                    ) from exc
                if destination.exists():
                    raise ProjectError(
                        f"Cannot Save As because an asset already exists at: {destination}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
                copied.append(destination)
        except Exception as exc:
            for destination in reversed(copied):
                try:
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, ProjectError):
                raise
            raise ProjectError(f"Could not copy project assets during Save As: {exc}") from exc
        return copied

    def save_autosave(self) -> Path:
        if self.file_path is None:
            raise ProjectError("Save the project once before enabling autosave")
        destination = self.autosave_path
        assert destination is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _utc_now()
        self._write_atomic(destination)
        return destination

    def _write_atomic(self, destination: Path) -> None:
        try:
            payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        except (TypeError, ValueError) as exc:
            raise ProjectError(f"Project data is not JSON serializable: {exc}") from exc
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n",
                dir=destination.parent, prefix=f".{destination.name}.",
                suffix=".tmp", delete=False,
            ) as stream:
                temp_name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, destination)
        except OSError as exc:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise ProjectError(f"Could not save project {destination}: {exc}") from exc

    def _copy_asset_into_project(self, source: Path, kind: str) -> tuple[str, int | None]:
        if self.file_path is None:
            raise ProjectError("Save the project before copying assets into it")
        root = self.file_path.parent.resolve()
        assets_root = root / "assets"
        folder = assets_root / kind

        # Do not follow a pre-existing symlink out of the project directory.
        # A downloaded project may contain a crafted assets/ or assets/<kind>
        # symlink; copying through it would otherwise write outside the project.
        for candidate in (assets_root, folder):
            if candidate.exists() or candidate.is_symlink():
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise ProjectError(
                        f"Asset storage path escapes the project folder: {candidate}"
                    ) from exc

        folder.mkdir(parents=True, exist_ok=True)
        resolved_folder = folder.resolve()
        try:
            resolved_folder.relative_to(root)
        except ValueError as exc:
            raise ProjectError(
                f"Asset storage path escapes the project folder: {folder}"
            ) from exc

        filename = _safe_asset_name(source.name)
        destination = folder / filename
        stem, suffix = destination.stem, destination.suffix
        counter = 2
        while destination.exists() or destination.is_symlink():
            destination = folder / f"{stem}_{counter}{suffix}"
            counter += 1
        try:
            destination.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise ProjectError(
                f"Asset destination escapes the project folder: {destination}"
            ) from exc

        try:
            if source.is_dir():
                shutil.copytree(source, destination)
                size_bytes = None
            else:
                shutil.copy2(source, destination)
                size_bytes = destination.stat().st_size
        except OSError as exc:
            raise ProjectError(f"Could not copy asset {source}: {exc}") from exc
        return destination.relative_to(root).as_posix(), size_bytes

    def add_asset(
        self,
        source_path: str | Path,
        kind: str,
        *,
        copy_into_project: bool = False,
    ) -> ProjectAsset:
        source = Path(source_path).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"Asset does not exist: {source}")
        if not source.is_file() and not source.is_dir():
            raise ProjectError(f"Unsupported asset type: {source}")
        if kind not in ASSET_KINDS:
            raise ProjectError(f"Unsupported asset kind: {kind}")

        resolved_source = source.resolve()
        for existing in self.assets:
            if existing.kind != kind:
                continue
            try:
                existing_path = self.resolve_asset(existing)
            except (OSError, ProjectError):
                continue
            if existing_path == resolved_source:
                # Opening a video first creates a linked reference. Let a later
                # explicit "copy into project" action promote that same entry.
                if copy_into_project and not existing.managed:
                    existing.path, existing.size_bytes = self._copy_asset_into_project(
                        resolved_source, kind
                    )
                    existing.managed = True
                return existing

        if copy_into_project:
            stored_path, size_bytes = self._copy_asset_into_project(resolved_source, kind)
            managed = True
        else:
            stored_path = str(resolved_source)
            size_bytes = resolved_source.stat().st_size if resolved_source.is_file() else None
            managed = False

        asset = ProjectAsset(
            id=uuid.uuid4().hex,
            name=source.name,
            kind=kind,
            path=stored_path,
            managed=managed,
            size_bytes=size_bytes,
        )
        self.assets.append(asset)
        return asset

    def _resolve_managed_asset_path(self, asset: ProjectAsset) -> Path:
        """Resolve only paths inside the project's dedicated assets/<kind>/ tree."""
        if self.file_path is None:
            raise ProjectError("Cannot resolve managed assets before the project is saved")
        relative = PurePosixPath(asset.path)
        windows_path = PureWindowsPath(asset.path)
        if ".." in relative.parts or ".." in windows_path.parts:
            raise ProjectError(f"Managed asset path contains traversal components: {asset.path}")
        if "\\" in asset.path:
            raise ProjectError("Managed asset paths must use project-relative POSIX separators")
        if relative.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ProjectError("Managed assets must use project-relative paths")
        parts = relative.parts
        if (
            len(parts) < 3
            or parts[0] != "assets"
            or parts[1] not in ASSET_KINDS
            or any(part in {"", "."} for part in parts)
        ):
            raise ProjectError(
                "Managed assets must be stored under assets/<kind>/ and cannot reference project files"
            )

        root = self.file_path.parent.resolve()
        kind_root = root / "assets" / parts[1]
        candidate = root.joinpath(*parts)

        # Reject links at every level, including the leaf, instead of following
        # project-controlled paths to other files.
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ProjectError("Managed asset paths cannot traverse symbolic links")

        resolved = candidate.resolve()
        try:
            resolved.relative_to(kind_root.resolve())
        except ValueError as exc:
            raise ProjectError("Managed asset path escapes its assets/<kind> directory") from exc
        if resolved == self.file_path.resolve():
            raise ProjectError("Refusing to treat the project document as a managed asset")
        return resolved

    def resolve_asset(self, asset: ProjectAsset | str) -> Path:
        selected = self.get_asset(asset) if isinstance(asset, str) else asset
        if selected is None:
            raise KeyError(f"Unknown asset: {asset}")
        if selected.managed:
            return self._resolve_managed_asset_path(selected)

        stored = Path(selected.path).expanduser()
        windows_path = PureWindowsPath(selected.path)
        if stored.is_absolute():
            return stored.resolve()
        if windows_path.is_absolute() or windows_path.drive:
            # Keep paths authored on Windows recognizable when opened elsewhere.
            return stored
        if ".." in PurePosixPath(selected.path).parts or ".." in windows_path.parts:
            raise ProjectError(f"Asset path contains traversal components: {selected.path}")
        if self.file_path is None:
            raise ProjectError("Cannot resolve relative assets before the project is saved")
        root = self.file_path.parent.resolve()
        resolved = (root / stored).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ProjectError(f"Asset path escapes the project folder: {selected.path}") from exc
        return resolved

    def get_asset(self, asset_id: str) -> ProjectAsset | None:
        return next((asset for asset in self.assets if asset.id == asset_id), None)

    def remove_asset(self, asset_id: str, *, delete_managed_file: bool = False) -> bool:
        asset = self.get_asset(asset_id)
        if asset is None:
            return False
        if delete_managed_file and asset.managed:
            resolved = self._resolve_managed_asset_path(asset)
            if resolved.exists():
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()
        self.assets = [item for item in self.assets if item.id != asset_id]
        return True

    def missing_assets(self) -> list[ProjectAsset]:
        missing: list[ProjectAsset] = []
        for asset in self.assets:
            try:
                if not self.resolve_asset(asset).exists():
                    missing.append(asset)
            except (OSError, ValueError):
                missing.append(asset)
        return missing

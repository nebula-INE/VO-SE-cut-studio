"""Resolve resources from source checkouts and PyInstaller one-folder bundles."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


def runtime_roots(
    *,
    module_file: str | Path,
    executable: str | Path,
    frozen_root: str | Path | None = None,
) -> list[Path]:
    """Return existing-layout roots in bundle-first order, without duplicates."""
    candidates: list[Path] = []
    if frozen_root is not None:
        candidates.append(Path(frozen_root))
    if executable:
        candidates.append(Path(executable).resolve().parent)

    module_path = Path(module_file).resolve()
    candidates.extend((module_path.parent.parent, module_path.parent))

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.resolve())
        if normalized not in seen:
            seen.add(normalized)
            roots.append(candidate.resolve())
    return roots


def find_runtime_file(
    filenames: str | Iterable[str],
    *,
    module_file: str | Path,
    executable: str | Path,
    frozen_root: str | Path | None = None,
    subdirectories: Iterable[str] = (),
) -> Path | None:
    """Find a bundled file, preferring frozen resources over source build paths."""
    names = (filenames,) if isinstance(filenames, str) else tuple(filenames)
    roots = runtime_roots(
        module_file=module_file,
        executable=executable,
        frozen_root=frozen_root,
    )
    for root in roots:
        for subdirectory in subdirectories:
            for name in names:
                candidate = root / subdirectory / name
                if candidate.is_file():
                    return candidate.resolve()
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate.resolve()
    return None

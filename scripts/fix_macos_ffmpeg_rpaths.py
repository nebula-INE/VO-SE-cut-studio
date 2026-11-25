#!/usr/bin/env python3
"""Isolate only aural_engine's FFmpeg dependency closure on macOS.

PySide6 bundles its own Qt and Shiboken dylibs in the Frameworks tree. Moving
all top-level dylibs breaks those packages because their extensions expect
sibling libraries at the original locations. This script instead builds a
private dependency closure starting at aural_engine's FFmpeg load commands,
copies only that closure to AxralNative/, and rewrites only the native engine
and its private dependencies to use @loader_path.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PRIVATE_DIR = "AxralNative"
FFMPEG_PREFIXES = ("libav", "libsw")


def run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    return result.stdout


def dependency_paths(binary: Path) -> list[str]:
    output = run(["otool", "-L", str(binary)])
    dependencies: list[str] = []
    for line in output.splitlines()[1:]:
        value = line.strip()
        if value:
            dependency = value.split(" (", 1)[0]
            if dependency:
                dependencies.append(dependency)
    return dependencies


def candidate_for_dependency(
    dependency: str,
    *,
    binary: Path,
    frameworks: Path,
    private_dir: Path,
    ffmpeg_lib_dir: Path,
) -> Path | None:
    """Resolve an install name to an available local or Homebrew dylib."""
    basename = Path(dependency).name
    if not basename or basename in {".", ".."}:
        return None

    candidates: list[Path] = []
    if dependency.startswith("@loader_path/"):
        candidates.append(binary.parent / dependency.removeprefix("@loader_path/"))
    elif dependency.startswith("@executable_path/"):
        candidates.append(
            binary.parents[2] / "MacOS" / dependency.removeprefix("@executable_path/")
        )
    elif dependency.startswith("@rpath/"):
        candidates.extend((
            private_dir / basename,
            ffmpeg_lib_dir / basename,
            frameworks / basename,
        ))
    elif dependency.startswith("/"):
        candidates.append(Path(dependency))
        candidates.extend((
            ffmpeg_lib_dir / basename,
            frameworks / basename,
        ))
    else:
        candidates.extend((
            private_dir / basename,
            ffmpeg_lib_dir / basename,
            frameworks / basename,
        ))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def copy_library(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return False
    shutil.copy2(source.resolve(), destination)
    print(f"[macos-native] Isolated {destination.name}")
    return True


def collect_ffmpeg_dependency_closure(
    engine_extensions: list[Path],
    frameworks: Path,
    private_dir: Path,
    ffmpeg_lib_dir: Path,
) -> set[str]:
    """Copy FFmpeg roots and their transitive non-system dependencies."""
    if not ffmpeg_lib_dir.is_dir():
        raise RuntimeError(
            f"Homebrew FFmpeg library directory was not found: {ffmpeg_lib_dir}. "
            "Set AXRAL_FFMPEG_LIB_DIR to the FFmpeg formula's lib directory."
        )

    pending: list[Path] = []
    moved_names: set[str] = set()

    for extension in engine_extensions:
        for dependency in dependency_paths(extension):
            basename = Path(dependency).name
            if not basename.startswith(FFMPEG_PREFIXES):
                continue
            source = candidate_for_dependency(
                dependency,
                binary=extension,
                frameworks=frameworks,
                private_dir=private_dir,
                ffmpeg_lib_dir=ffmpeg_lib_dir,
            )
            if source is None:
                raise RuntimeError(
                    f"Could not resolve FFmpeg dependency {dependency} "
                    f"from {extension.name}"
                )
            destination = private_dir / basename
            copy_library(source, destination)
            if basename not in moved_names:
                moved_names.add(basename)
                pending.append(destination)

    if not moved_names:
        raise RuntimeError("aural_engine has no direct FFmpeg dylib dependencies")

    visited: set[str] = set()
    while pending:
        current = pending.pop()
        current_name = current.name
        if current_name in visited:
            continue
        visited.add(current_name)

        for dependency in dependency_paths(current):
            basename = Path(dependency).name
            if (
                not basename
                or basename in {".", ".."}
                or dependency.startswith(("/System/Library/", "/usr/lib/"))
                or basename in moved_names
            ):
                continue

            source = candidate_for_dependency(
                dependency,
                binary=current,
                frameworks=frameworks,
                private_dir=private_dir,
                ffmpeg_lib_dir=ffmpeg_lib_dir,
            )
            if source is None:
                # Keep non-FFmpeg system and Qt-managed dependencies at their
                # original locations. Only copy load commands that resolve to
                # a concrete library staged by PyInstaller or Homebrew.
                continue

            destination = private_dir / basename
            if copy_library(source, destination):
                pending.append(destination)
            moved_names.add(basename)

    return moved_names


def install_change(binary: Path, old: str, new: str) -> None:
    if old != new:
        run(["install_name_tool", "-change", old, new, str(binary)])
        print(f"[macos-native] {binary.name}: {old} -> {new}")


def patch_private_dependencies(private_dir: Path, moved_names: set[str]) -> int:
    changed = 0
    for binary in sorted(private_dir.iterdir()):
        if not binary.is_file() or binary.suffix.lower() != ".dylib":
            continue
        for dependency in dependency_paths(binary):
            basename = Path(dependency).name
            if basename in moved_names:
                install_change(binary, dependency, f"@loader_path/{basename}")
                changed += 1
    return changed


def patch_engine_extensions(
    engine_extensions: list[Path],
    moved_names: set[str],
) -> int:
    changed = 0
    for binary in engine_extensions:
        for dependency in dependency_paths(binary):
            basename = Path(dependency).name
            if basename in moved_names:
                install_change(
                    binary,
                    dependency,
                    f"@loader_path/{PRIVATE_DIR}/{basename}",
                )
                changed += 1
    return changed


def patch_bundle(app_bundle: Path, ffmpeg_lib_dir: Path) -> int:
    app_bundle = app_bundle.expanduser().resolve()
    frameworks = app_bundle / "Contents" / "Frameworks"
    if not frameworks.is_dir():
        raise RuntimeError(f"PyInstaller Frameworks directory is missing: {frameworks}")

    engine_extensions = sorted(frameworks.glob("aural_engine*.so"))
    if not engine_extensions:
        raise RuntimeError(
            f"aural_engine extension was not found at the Frameworks root: {frameworks}"
        )

    private_dir = frameworks / PRIVATE_DIR
    moved_names = collect_ffmpeg_dependency_closure(
        engine_extensions,
        frameworks,
        private_dir,
        ffmpeg_lib_dir,
    )
    changes = patch_private_dependencies(private_dir, moved_names)
    changes += patch_engine_extensions(engine_extensions, moved_names)

    # install_name_tool invalidates PyInstaller's ad-hoc signatures.
    run([
        "codesign", "--force", "--deep", "--sign", "-", "--timestamp=none",
        str(app_bundle),
    ])
    print(
        f"[macos-native] Isolated {len(moved_names)} FFmpeg dependency dylib(s), "
        f"rewrote {changes} load command(s), and re-signed {app_bundle}"
    )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app_bundle", type=Path)
    parser.add_argument(
        "--ffmpeg-lib-dir",
        type=Path,
        default=Path(os.environ["AXRAL_FFMPEG_LIB_DIR"])
        if os.environ.get("AXRAL_FFMPEG_LIB_DIR")
        else None,
    )
    args = parser.parse_args()
    if args.ffmpeg_lib_dir is None:
        raise RuntimeError("--ffmpeg-lib-dir or AXRAL_FFMPEG_LIB_DIR is required")
    patch_bundle(args.app_bundle, args.ffmpeg_lib_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"[macos-native] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

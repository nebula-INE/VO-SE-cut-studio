#!/usr/bin/env python3
"""Isolate PyInstaller's Homebrew dylibs from PySide6's private Qt libraries.

PyInstaller may flatten CMake/Homebrew dependencies into Contents/Frameworks.
Qt's own FFmpeg libraries live below PySide6/Qt/lib and can have identical
basenames but different ABIs. Move the top-level external dylib set into a
private subdirectory, copy the direct FFmpeg libraries from Homebrew when a
basename was deduplicated, and rewrite consumers to explicit @loader_path loads.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PRIVATE_DIR = "AxralNative"
FFMPEG_PATTERNS = ("libav*.dylib", "libsw*.dylib")


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


def install_change(binary: Path, old: str, new: str) -> None:
    if old != new:
        run(["install_name_tool", "-change", old, new, str(binary)])
        print(f"[macos-native] {binary.name}: {old} -> {new}")


def copy_homebrew_ffmpeg_libraries(private_dir: Path, source_dir: Path) -> None:
    if not source_dir.is_dir():
        raise RuntimeError(
            f"Homebrew FFmpeg library directory was not found: {source_dir}. "
            "Set AXRAL_FFMPEG_LIB_DIR to the FFmpeg formula's lib directory."
        )
    for pattern in FFMPEG_PATTERNS:
        for source in sorted(source_dir.glob(pattern)):
            if not source.is_file():
                continue
            destination = private_dir / source.name
            if not destination.exists():
                shutil.copy2(source.resolve(), destination)
                print(f"[macos-native] Added Homebrew library: {destination.name}")


def move_top_level_dylibs(frameworks: Path, private_dir: Path) -> set[str]:
    private_dir.mkdir(parents=True, exist_ok=True)
    moved: set[str] = set()
    for source in sorted(frameworks.iterdir()):
        if not source.is_file() or source.suffix.lower() != ".dylib":
            continue
        destination = private_dir / source.name
        if destination.exists():
            source.unlink()
        else:
            # Resolve symlinks before moving: Homebrew and PyInstaller may stage
            # versioned dylib symlinks whose relative targets change after a move.
            shutil.copy2(source.resolve(), destination)
            source.unlink()
        moved.add(destination.name)
        print(f"[macos-native] Isolated {destination.name}")
    return moved


def patch_moved_library_dependencies(private_dir: Path, moved_names: set[str]) -> int:
    changed = 0
    for binary in sorted(private_dir.glob("*.dylib")):
        for dependency in dependency_paths(binary):
            basename = Path(dependency).name
            if basename in moved_names:
                install_change(binary, dependency, f"@loader_path/{basename}")
                changed += 1
    return changed


def patch_framework_consumers(frameworks: Path, moved_names: set[str]) -> int:
    changed = 0
    for binary in sorted(frameworks.iterdir()):
        if not binary.is_file() or binary.suffix.lower() not in {".so", ".dylib"}:
            continue
        for dependency in dependency_paths(binary):
            basename = Path(dependency).name
            if basename in moved_names:
                install_change(binary, dependency, f"@loader_path/{PRIVATE_DIR}/{basename}")
                changed += 1
    return changed


def patch_app_executable(app_bundle: Path, moved_names: set[str]) -> int:
    macos_dir = app_bundle / "Contents" / "MacOS"
    changed = 0
    if not macos_dir.is_dir():
        return changed
    for binary in sorted(macos_dir.iterdir()):
        if not binary.is_file():
            continue
        for dependency in dependency_paths(binary):
            basename = Path(dependency).name
            if basename in moved_names:
                install_change(
                    binary,
                    dependency,
                    f"@loader_path/../Frameworks/{PRIVATE_DIR}/{basename}",
                )
                changed += 1
    return changed


def patch_bundle(app_bundle: Path, ffmpeg_lib_dir: Path) -> int:
    app_bundle = app_bundle.expanduser().resolve()
    frameworks = app_bundle / "Contents" / "Frameworks"
    if not frameworks.is_dir():
        raise RuntimeError(f"PyInstaller Frameworks directory is missing: {frameworks}")

    private_dir = frameworks / PRIVATE_DIR
    moved_names = move_top_level_dylibs(frameworks, private_dir)
    copy_homebrew_ffmpeg_libraries(private_dir, ffmpeg_lib_dir)
    # Include copied FFmpeg dylibs even if PyInstaller had deduplicated them
    # against Qt's private libraries and therefore did not stage them at root.
    moved_names.update(path.name for path in private_dir.glob("*.dylib"))

    changes = patch_moved_library_dependencies(private_dir, moved_names)
    changes += patch_framework_consumers(frameworks, moved_names)
    changes += patch_app_executable(app_bundle, moved_names)

    # install_name_tool invalidates PyInstaller's ad-hoc signatures.
    run([
        "codesign", "--force", "--deep", "--sign", "-", "--timestamp=none",
        str(app_bundle),
    ])
    print(
        f"[macos-native] Isolated {len(moved_names)} dylib(s), rewrote "
        f"{changes} load command(s), and re-signed {app_bundle}"
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

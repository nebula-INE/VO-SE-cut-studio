#!/usr/bin/env python3
"""Make bundled macOS dylibs resolve sibling libraries before global @rpath entries.

PySide6 ships private FFmpeg dylibs inside its Qt directory. The native aural_engine
extension can link against Homebrew FFmpeg with the same dylib basenames. PyInstaller
places the Homebrew dependencies beside the extension, but Qt's @rpath entries can
win the lookup and cause ABI/symbol errors. Rewriting sibling dependencies to
@loader_path keeps each FFmpeg family together.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


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


def mach_o_candidates(app_bundle: Path) -> list[Path]:
    frameworks = app_bundle / "Contents" / "Frameworks"
    macos_dir = app_bundle / "Contents" / "MacOS"
    if not frameworks.is_dir():
        raise RuntimeError(f"PyInstaller Frameworks directory is missing: {frameworks}")

    candidates = [
        path for path in frameworks.rglob("*")
        if path.is_file() and path.suffix.lower() in {".dylib", ".so"}
    ]
    if macos_dir.is_dir():
        candidates.extend(path for path in macos_dir.iterdir() if path.is_file())
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique[str(candidate.resolve())] = candidate
    return list(unique.values())


def dependency_paths(binary: Path) -> list[str]:
    output = run(["otool", "-L", str(binary)])
    dependencies: list[str] = []
    # The first line names the inspected binary; the rest are load commands.
    for line in output.splitlines()[1:]:
        value = line.strip()
        if not value:
            continue
        dependency = value.split(" (", 1)[0]
        if dependency:
            dependencies.append(dependency)
    return dependencies


def patch_bundle(app_bundle: Path) -> int:
    app_bundle = app_bundle.expanduser().resolve()
    changed = 0
    for binary in mach_o_candidates(app_bundle):
        for dependency in dependency_paths(binary):
            basename = Path(dependency).name
            if not basename or basename in {".", ".."}:
                continue
            sibling = binary.parent / basename
            replacement = f"@loader_path/{basename}"
            if not sibling.is_file() or dependency == replacement:
                continue
            run([
                "install_name_tool",
                "-change",
                dependency,
                replacement,
                str(binary),
            ])
            print(f"[macos-rpath] {binary.name}: {dependency} -> {replacement}")
            changed += 1

    # install_name_tool invalidates PyInstaller's ad-hoc signatures.
    run(["codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", str(app_bundle)])
    print(f"[macos-rpath] Patched {changed} dependency reference(s) and re-signed {app_bundle}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app_bundle", type=Path)
    args = parser.parse_args()
    patch_bundle(args.app_bundle)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"[macos-rpath] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

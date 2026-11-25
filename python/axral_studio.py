"""Axral Cut Studio application entry point and native DLL bootstrap."""
from __future__ import annotations

import os
import sys
from pathlib import Path


_DLL_DIRECTORY_HANDLES: list[object] = []


def configure_runtime_search_paths() -> None:
    """Expose bundled or source-build native libraries to the OS loader."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    directories: list[Path] = []
    if frozen_root:
        bundle = Path(frozen_root)
        directories.extend((bundle / "native", bundle / "bin"))
    directories.append(Path(sys.executable).resolve().parent)

    if not getattr(sys, "frozen", False):
        source_root = Path(__file__).resolve().parent.parent
        directories.extend((
            source_root / "build",
            source_root / "build" / "Release",
            source_root / "vcpkg" / "installed" / "x64-windows" / "bin",
            source_root / "vcpkg_installed" / "x64-windows" / "bin",
        ))

    search_paths: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        value = str(directory.resolve())
        if value in search_paths:
            continue
        search_paths.append(value)
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            # Keep the handle alive; closing it removes the DLL search path.
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(value))

    if search_paths:
        existing = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(
            search_paths + ([existing] if existing else [])
        )


def main() -> int:
    configure_runtime_search_paths()
    source_dir = Path(__file__).resolve().parent
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))

    if not getattr(sys, "frozen", False):
        source_root = source_dir.parent
        for build_dir in (source_root / "build" / "Release", source_root / "build"):
            if build_dir.is_dir() and str(build_dir) not in sys.path:
                sys.path.insert(0, str(build_dir))

    from main_window import main as run_main

    return run_main()


if __name__ == "__main__":
    raise SystemExit(main())

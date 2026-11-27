#!/usr/bin/env python3
"""Smoke-test the native binaries and extension staged by the CMake build."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"[runtime-smoke] ERROR: {message}")


def run_usage_probe(executable: Path) -> None:
    if not executable.is_file():
        fail(f"missing executable: {executable}")
    try:
        result = subprocess.run(
            [str(executable)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"could not start {executable.name}: {exc}")

    if result.returncode != 1 or "Usage:" not in result.stderr:
        fail(
            f"{executable.name} did not reach its usage handler "
            f"(exit={result.returncode}, stderr={result.stderr!r}, "
            f"stdout={result.stdout!r})"
        )
    print(f"[runtime-smoke] OK: {executable.name} loaded and reached main()")


def main() -> int:
    system = platform.system()
    build_dir = ROOT / "build"
    if system == "Windows":
        build_dir = build_dir / "Release"
        suffix = ".exe"
        core_name = "vose_core.dll"
        extension_pattern = "aural_engine*.pyd"
    elif system == "Darwin":
        suffix = ""
        core_name = "libvose_core.dylib"
        extension_pattern = "aural_engine*.so"
    elif system == "Linux":
        suffix = ""
        core_name = "libvose_core.so"
        extension_pattern = "aural_engine*.so"
    else:
        fail(f"unsupported platform: {system}")

    core_path = build_dir / core_name
    if not core_path.is_file():
        fail(f"missing VO-SE runtime library: {core_path}")
    try:
        import ctypes
        dll_directory = os.add_dll_directory(str(build_dir)) if system == "Windows" else None
        ctypes.CDLL(str(core_path.resolve()))
        # Keep the Windows DLL search handle alive for the rest of this smoke run.
        _ = dll_directory
    except OSError as exc:
        fail(f"could not load {core_path.name}: {exc}")
    print(f"[runtime-smoke] OK: {core_name} loaded")

    run_usage_probe(build_dir / f"aural-engine{suffix}")
    run_usage_probe(build_dir / f"aural-compose{suffix}")

    extensions = sorted(build_dir.glob(extension_pattern))
    if not extensions:
        fail(f"missing Python extension matching {extension_pattern} in {build_dir}")

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(build_dir) if not existing_pythonpath
        else os.pathsep.join((str(build_dir), existing_pythonpath))
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import aural_engine; "
                "assert callable(aural_engine.VideoDecoder), "
                "'VideoDecoder binding is missing'",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"could not import aural_engine extension: {exc}")
    if result.returncode != 0:
        fail(
            "aural_engine import failed "
            f"(exit={result.returncode}, stderr={result.stderr!r}, "
            f"stdout={result.stdout!r})"
        )
    print(f"[runtime-smoke] OK: {extensions[0].name} imported")
    print("[runtime-smoke] All native runtime checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

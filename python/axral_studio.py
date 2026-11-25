"""Axral Cut Studio application entry point and native DLL bootstrap."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_runtime_search_paths() -> None:
    """Expose bundled native libraries and command-line tools to the OS loader."""
    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    roots.append(Path(sys.executable).resolve().parent)

    search_paths: list[str] = []
    for root in roots:
        for child in ("native", "bin"):
            directory = root / child
            if directory.is_dir():
                value = str(directory.resolve())
                if value not in search_paths:
                    search_paths.append(value)
                if os.name == "nt" and hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(value)

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

    from main_window import main as run_main

    return run_main()


if __name__ == "__main__":
    raise SystemExit(main())

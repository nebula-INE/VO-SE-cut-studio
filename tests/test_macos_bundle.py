from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fix_macos_ffmpeg_rpaths.py"
SPEC = importlib.util.spec_from_file_location("fix_macos_ffmpeg_rpaths", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
macos_bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_bundle)


def test_ffmpeg_dependency_closure_does_not_move_qt_or_shiboken_libraries(
    tmp_path: Path, monkeypatch
) -> None:
    frameworks = tmp_path / "Frameworks"
    frameworks.mkdir()
    engine = frameworks / "aural_engine.cpython-312-darwin.so"
    engine.write_bytes(b"engine")
    # This file belongs to PySide6 and must remain at its original location.
    shiboken = frameworks / "libshiboken6.abi3.6.12.dylib"
    shiboken.write_bytes(b"shiboken")

    homebrew_lib = tmp_path / "homebrew" / "ffmpeg" / "lib"
    homebrew_lib.mkdir(parents=True)
    for name in (
        "libavfilter.12.dylib",
        "libavcodec.63.dylib",
        "libssl.4.dylib",
        "libcrypto.4.dylib",
    ):
        (homebrew_lib / name).write_bytes(name.encode("utf-8"))

    private_dir = frameworks / "AxralNative"
    dependencies = {
        engine.name: ["@rpath/libavfilter.12.dylib"],
        "libavfilter.12.dylib": [
            "@rpath/libavcodec.63.dylib",
            "@rpath/libssl.4.dylib",
        ],
        "libavcodec.63.dylib": ["/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"],
        "libssl.4.dylib": ["@rpath/libcrypto.4.dylib"],
        "libcrypto.4.dylib": ["/usr/lib/libSystem.B.dylib"],
    }

    monkeypatch.setattr(
        macos_bundle,
        "dependency_paths",
        lambda binary: dependencies.get(binary.name, []),
    )

    moved = macos_bundle.collect_ffmpeg_dependency_closure(
        [engine],
        frameworks,
        private_dir,
        homebrew_lib,
    )

    assert moved == {
        "libavfilter.12.dylib",
        "libavcodec.63.dylib",
        "libssl.4.dylib",
        "libcrypto.4.dylib",
    }
    assert sorted(path.name for path in private_dir.iterdir()) == sorted(moved)
    assert shiboken.is_file()
    assert not (private_dir / shiboken.name).exists()

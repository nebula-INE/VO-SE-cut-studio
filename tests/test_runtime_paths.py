from __future__ import annotations

from pathlib import Path

from runtime_paths import find_runtime_file, runtime_roots


def test_runtime_roots_prioritize_frozen_bundle(tmp_path: Path) -> None:
    frozen = tmp_path / "bundle" / "_internal"
    executable = tmp_path / "bundle" / "AxralCutStudio"
    module_file = tmp_path / "source" / "python" / "runtime_paths.py"

    roots = runtime_roots(
        module_file=module_file,
        executable=executable,
        frozen_root=frozen,
    )

    assert roots[0] == frozen
    assert roots[1] == executable.parent
    assert len(roots) == len(set(roots))


def test_find_runtime_file_finds_native_bundle_resource(tmp_path: Path) -> None:
    frozen = tmp_path / "bundle" / "_internal"
    native_dir = frozen / "native"
    native_dir.mkdir(parents=True)
    library = native_dir / "libvose_core.so"
    library.write_bytes(b"test-placeholder")

    found = find_runtime_file(
        "libvose_core.so",
        module_file=tmp_path / "source" / "python" / "main_window.py",
        executable=tmp_path / "bundle" / "AxralCutStudio",
        frozen_root=frozen,
        subdirectories=("native", "build", "build/Release"),
    )

    assert found == library.resolve()


def test_find_runtime_file_finds_bundled_ffmpeg(tmp_path: Path) -> None:
    frozen = tmp_path / "bundle" / "_internal"
    bin_dir = frozen / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_bytes(b"test-placeholder")

    found = find_runtime_file(
        ("ffmpeg", "ffmpeg.exe"),
        module_file=tmp_path / "source" / "python" / "media_ffmpeg.py",
        executable=tmp_path / "bundle" / "AxralCutStudio",
        frozen_root=frozen,
        subdirectories=("bin",),
    )

    assert found == ffmpeg.resolve()


def test_find_runtime_file_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert find_runtime_file(
        "missing.bin",
        module_file=tmp_path / "source" / "python" / "main_window.py",
        executable=tmp_path / "bundle" / "AxralCutStudio",
        frozen_root=tmp_path / "bundle" / "_internal",
        subdirectories=("native",),
    ) is None

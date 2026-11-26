#!/usr/bin/env python3
"""Build and validate a one-folder Axral Cut Studio distribution."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
RELEASE = ROOT / "release"
APP_NAME = "AxralCutStudio"
VERSION = os.environ.get("AXRAL_PACKAGE_VERSION", "0.1.0")


def native_build_dir() -> Path:
    return ROOT / "build" / "Release" if os.name == "nt" else ROOT / "build"


def find_native_library() -> Path:
    build = native_build_dir()
    names = (
        ("vose_core.dll", "libvose_core.dll") if os.name == "nt"
        else ("libvose_core.dylib",) if sys.platform == "darwin"
        else ("libvose_core.so",)
    )
    for name in names:
        candidate = build / name
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(f"VO-SE native library not found in {build}; build CMake targets first")


def find_ffmpeg_tool(name: str) -> Path:
    executable_name = f"{name}.exe" if os.name == "nt" else name
    search_dirs: list[Path] = []
    configured = os.environ.get("AXRAL_FFMPEG_DIR")
    if configured:
        search_dirs.append(Path(configured))
    search_dirs.extend((
        ROOT / "vcpkg/installed/x64-windows/tools/ffmpeg",
        ROOT / "vcpkg/installed/x64-windows/tools/ffmpeg/bin",
        ROOT / "vcpkg/installed/x64-windows/bin",
        ROOT / "vcpkg_installed/x64-windows/tools/ffmpeg",
        ROOT / "vcpkg_installed/x64-windows/tools/ffmpeg/bin",
    ))
    for directory in search_dirs:
        candidate = directory / executable_name
        if candidate.is_file():
            return candidate.resolve()
    located = shutil.which(executable_name) or shutil.which(name)
    if located:
        return Path(located).resolve()
    raise RuntimeError(
        f"{executable_name} was not found. Install FFmpeg/FFprobe or set AXRAL_FFMPEG_DIR."
    )


def add_binary(arguments: list[str], source: Path, destination: str) -> None:
    arguments.extend(("--add-binary", f"{source}{os.pathsep}{destination}"))


def build_application() -> Path:
    system = platform.system()
    build_dir = native_build_dir()
    native_library = find_native_library()
    ffmpeg = find_ffmpeg_tool("ffmpeg")
    ffprobe = find_ffmpeg_tool("ffprobe")

    arguments = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--paths", str(ROOT / "python"),
        "--paths", str(build_dir),
        "--hidden-import", "aural_engine",
        "--hidden-import", "PySide6.QtOpenGLWidgets",
        "--hidden-import", "PySide6.QtWebEngineWidgets",
        "--hidden-import", "PySide6.QtWebEngineCore",
        "--collect-all", "PySide6.QtWebEngineWidgets",
        "--collect-all", "PySide6.QtWebEngineCore",
        "--collect-all", "pyopenjtalk",
        "--hidden-import", "OpenGL.GL",
    ]
    viewer_dist = ROOT / "web" / "vrm_viewer" / "dist"
    viewer_index = viewer_dist / "index.html"
    if not viewer_index.is_file():
        raise RuntimeError(
            "VRM viewer assets are missing. Run npm install and npm run build "
            "inside web/vrm_viewer before packaging."
        )
    arguments.extend(("--add-data", f"{viewer_dist}{os.pathsep}vrm_viewer"))

    add_binary(arguments, native_library, "native")
    # Homebrew FFmpeg uses dylib basenames that can collide with Qt's private
    # FFmpeg build inside PySide6. On macOS use the user's Homebrew installation.
    if sys.platform != "darwin":
        add_binary(arguments, ffmpeg, "bin")
        add_binary(arguments, ffprobe, "bin")

    if os.name == "nt":
        dll_dir = ROOT / "vcpkg/installed/x64-windows/bin"
        if not dll_dir.is_dir():
            dll_dir = ROOT / "vcpkg_installed/x64-windows/bin"
        if dll_dir.is_dir():
            for dll in sorted(dll_dir.glob("*.dll")):
                add_binary(arguments, dll.resolve(), "bin")

    arguments.append(str(ROOT / "python" / "axral_studio.py"))
    print("Running:", " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=ROOT, check=True)

    if system == "Windows":
        app_dir = DIST / APP_NAME
        executable = app_dir / f"{APP_NAME}.exe"
    elif system == "Darwin":
        app_dir = DIST / f"{APP_NAME}.app"
        executable = app_dir / "Contents" / "MacOS" / APP_NAME
    else:
        app_dir = DIST / APP_NAME
        executable = app_dir / APP_NAME

    if not executable.is_file():
        raise RuntimeError(f"Packaged executable was not found: {executable}")
    if sys.platform == "darwin":
        patch_env = os.environ.copy()
        patch_env["AXRAL_FFMPEG_LIB_DIR"] = str(ffmpeg.resolve().parent.parent / "lib")
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "fix_macos_ffmpeg_rpaths.py"),
                str(app_dir),
                "--ffmpeg-lib-dir",
                patch_env["AXRAL_FFMPEG_LIB_DIR"],
            ],
            cwd=ROOT,
            env=patch_env,
            check=True,
        )
    smoke = subprocess.run(
        [str(executable), "--runtime-check"],
        cwd=ROOT, env=os.environ.copy(), check=False,
        capture_output=True, text=True, timeout=90,
    )
    print(smoke.stdout, end="", flush=True)
    print(smoke.stderr, end="", file=sys.stderr, flush=True)
    if smoke.returncode != 0:
        raise RuntimeError(f"Packaged runtime smoke test failed (exit={smoke.returncode})")
    return app_dir


def make_archive(app_dir: Path) -> None:
    RELEASE.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        archive = RELEASE / f"{APP_NAME}-{VERSION}-windows-x64.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for path in app_dir.rglob("*"):
                if path.is_file():
                    output.write(path, path.relative_to(app_dir.parent))
    elif sys.platform == "darwin":
        archive = RELEASE / f"{APP_NAME}-{VERSION}-macos.zip"
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app_dir), str(archive)],
            check=True,
        )
        staging = RELEASE / "dmg-stage"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        shutil.copytree(app_dir, staging / app_dir.name)
        (staging / "Applications").symlink_to("/Applications")
        subprocess.run([
            "hdiutil", "create", "-volname", "Axral Cut Studio",
            "-srcfolder", str(staging), "-ov", "-format", "UDZO",
            str(RELEASE / f"{APP_NAME}-{VERSION}-macos.dmg"),
        ], check=True)
        shutil.rmtree(staging)
    else:
        archive = RELEASE / f"{APP_NAME}-{VERSION}-linux-x64.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(app_dir, arcname=app_dir.name)
        subprocess.run(
            ["bash", str(ROOT / "scripts" / "build_linux_deb.sh")],
            cwd=ROOT, env=os.environ.copy(), check=True,
        )


def build_windows_installer() -> None:
    if os.name != "nt":
        return
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates = [
        os.environ.get("ISCC_EXE"),
        shutil.which("ISCC.exe"),
        str(program_files_x86 / "Inno Setup 6" / "ISCC.exe"),
        str(program_files / "Inno Setup 6" / "ISCC.exe"),
    ]
    compiler = next((Path(item) for item in candidates if item and Path(item).is_file()), None)
    if compiler is None:
        raise RuntimeError("Inno Setup 6 was not found; install it before packaging Windows.")
    subprocess.run(
        [str(compiler), str(ROOT / "packaging" / "windows" / "AxralCutStudio.iss")],
        cwd=ROOT, check=True, env=os.environ.copy(),
    )


def main() -> int:
    RELEASE.mkdir(parents=True, exist_ok=True)
    app_dir = build_application()
    make_archive(app_dir)
    build_windows_installer()
    print(f"Distribution packages created in: {RELEASE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[package] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

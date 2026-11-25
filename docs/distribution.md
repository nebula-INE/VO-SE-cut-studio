# Axral Cut Studio distribution packages v1

このリポジトリの配布パイプラインは、Python/PySide6アプリ、`aural_engine` 拡張モジュール、VO-SEネイティブライブラリ、FFmpeg/FFprobeを1つの配布物へまとめます。音声合成・ミキシング・エクスポートのアルゴリズムは変更しません。

## CIが生成する配布物

| OS | 配布形式 | 内容 |
|---|---|---|
| Windows x64 | Inno Setup `.exe` と portable `.zip` | アプリ、Python/Qt実行環境、VO-SE DLL、FFmpeg/FFprobe、必要なvcpkg DLL |
| macOS | `.dmg` と `.zip` | アプリバンドル、Python/Qt実行環境、VO-SE dylib、FFmpeg/FFprobe |
| Linux x64 | `.deb` と `.tar.gz` | アプリ、Python/Qt実行環境、VO-SE共有ライブラリ、FFmpeg/FFprobe |

パッケージ生成前に各OSのCIでPythonテストを実行し、生成済みアプリの `--runtime-check` を実行します。検査ではGUIを開かず、バンドル内のimport、VO-SE共有ライブラリのロードとバージョン取得、FFmpeg/FFprobeの実行ファイル検出を確認します。

## ローカルでの生成

CMakeでネイティブターゲットをビルドし、`requirements.txt` と `requirements-dev.txt` に加えて `pybind11`、`pyinstaller` をインストールしてください。FFmpegとFFprobeもPATH上、または `AXRAL_FFMPEG_DIR` で指定したディレクトリに用意します。

Windowsではvcpkgの `ffmpeg:x64-windows` を使用し、FFmpeg DLLがある `installed/x64-windows/bin` を同梱します。ビルド後に次を実行します。

```bash
python scripts/package_application.py
```

任意のバージョン文字列は `AXRAL_PACKAGE_VERSION` で指定できます（既定値 `0.1.0`）。

## 既知の配布上の制約

- この段階ではWindowsのコード署名、macOSのDeveloper ID署名・Notarizationは行いません。署名のないmacOSアプリはGatekeeperの警告対象になる場合があります。
- Windowsはx64、Linuxはx86_64/amd64でビルドします。macOSはGitHub ActionsランナーのCPUアーキテクチャに対応するビルドです。
- Linux `.deb` はFFmpegとQtのX11/OpenGL/PulseAudio実行時ライブラリをAPT依存として宣言します。`.tar.gz` は同じOS依存関係を持つ環境向けです。
- LinuxのPyInstaller成果物はビルド時のglibcとの互換性に依存します。配布先の最小サポートOSを確定した後、より古い基準イメージでのビルドへ固定します。
- GPUドライバ、OS標準ライブラリ、ユーザーが読み込む外部音源・動画・画像素材はアプリへ含めません。
- FFmpegのコーデック構成・ライセンス表示は、利用するOS配布物と同梱バイナリのライセンス条件に従って確認してください。

## 起動時の調査

起動に失敗した場合は、配布物の実行ファイルを `--runtime-check` 付きで起動してください。CIでも同じ検査を実行します。ソースツリーから起動する場合は、`python python/axral_studio.py` を使用できます。

# FFmpegを使った音声デコード・MP4統合

aural Studio のタイムライン音声ミックスは、通常のPCM WAVならFFmpegなしでも書き出せます。MP3・FLAC・AAC/M4A・OGG・圧縮WAVなどのデコード、および既存映像へのMP4統合には、FFmpegのコマンドライン実行ファイルが必要です。

## FFmpegの設定

アプリは次の順でFFmpegを探します。

1. 環境変数 `FFMPEG_BINARY` に指定された実行ファイル
2. PATH上の `ffmpeg` / `ffmpeg.exe`
3. アプリ・リポジトリ内の一般的な同梱場所

見つからない場合は、OSに応じてFFmpegをインストールするか、実行ファイルの絶対パスを `FFMPEG_BINARY` に設定してください。

### Windows

FFmpegのWindowsビルドを用意し、例えば次のように設定します。

```powershell
$env:FFMPEG_BINARY = "C:\tools\ffmpeg\bin\ffmpeg.exe"
ffmpeg -version
```

FFmpegをアプリに同梱する場合は、`ffmpeg.exe` だけでなく、そのビルドが必要とするDLLも一緒に配置してください。

### macOS

Homebrewを利用する場合:

```bash
brew install ffmpeg
ffmpeg -version
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install ffmpeg
ffmpeg -version
```

## タイムライン音声の書き出し

「タイムライン → 音声ミックスをWAVに書き出す...」を選ぶと、有効でミュートされていないAudio/Voiceクリップをタイムライン位置に配置し、重なりを加算したステレオ48kHz/16bit PCM WAVを出力します。クリップの音量、フェードイン、フェードアウト、素材内の開始位置が反映されます。

PCM WAV以外の素材は、FFmpegでクリップ区間だけをステレオfloat PCMへデコードしてから同じミキサーで処理します。元の素材ファイルは変更しません。

## 完成映像への音声統合

「タイムライン → 完成映像にタイムライン音声を統合してMP4に書き出す...」を選び、先に合成しておいた映像ファイルと出力先を指定します。

- 映像ストリーム: 再エンコードせずコピー
- 音声ストリーム: AAC、標準192kbps
- 出力: MP4
- 映像・音声の短い方に合わせて終了

映像を再エンコードしないため、入力映像はMP4コンテナへストリームコピー可能なコーデックである必要があります。H.264映像を推奨します。未対応コーデックの場合は、H.264などの互換性がある形式で合成済み映像を作ってから再実行してください。

出力は一時ファイルに作成し、FFmpegが成功した場合だけ指定先へ置き換えます。選択した入力映像・タイムライン音声素材を出力先に指定して上書きすることはできません。

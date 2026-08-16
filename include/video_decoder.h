#pragma once

#include <cstdint>
#include <string>
#include <vector>

extern "C" {
struct AVFormatContext;
struct AVCodecContext;
struct AVFrame;
struct AVPacket;
struct SwsContext;
}

// デコード済み1フレーム分のRGBデータ
struct DecodedFrame {
    std::vector<uint8_t> rgb_data; // RGB24, tightly packed (width*height*3)
    int width = 0;
    int height = 0;
    double pts_seconds = 0.0;      // プレゼンテーションタイムスタンプ(秒)
};

// 動画ファイルを開き、フレームを1枚ずつRGB24でデコードして取り出すクラス。
// 使い方:
//   VideoDecoder dec;
//   if (!dec.open("input.mp4")) { ... }
//   DecodedFrame frame;
//   while (dec.decode_next_frame(frame)) {
//       // frame.rgb_data を texture_uploader に渡す
//   }
class VideoDecoder {
public:
    VideoDecoder();
    ~VideoDecoder();

    // コピー禁止（内部にFFmpegの生ポインタを保持するため）
    VideoDecoder(const VideoDecoder&) = delete;
    VideoDecoder& operator=(const VideoDecoder&) = delete;

    // 動画ファイルを開き、ビデオストリームとデコーダを初期化する。
    // 成功時 true、失敗時 false（内部でエラーメッセージを stderr に出力）。
    bool open(const std::string& filename);

    // 次の1フレームをデコードして out に格納する。
    // 動画の終端に達した、またはエラーの場合は false を返す。
    bool decode_next_frame(DecodedFrame& out);

    // 指定した秒数へシークする（キーフレーム単位、厳密ではない）。
    bool seek(double seconds);

    // 動画の総時間(秒)。取得できない場合は 0。
    double duration_seconds() const;

    int width() const { return width_; }
    int height() const { return height_; }
    double fps() const { return fps_; }

    // 開いたリソースを解放する。デストラクタからも呼ばれる。
    void close();

    bool is_open() const { return fmt_ctx_ != nullptr; }

private:
    bool init_sws_context();

    AVFormatContext* fmt_ctx_ = nullptr;
    AVCodecContext* codec_ctx_ = nullptr;
    AVFrame* frame_ = nullptr;      // デコード直後のフレーム(元フォーマット)
    AVFrame* rgb_frame_ = nullptr;  // RGB24変換後のフレーム
    AVPacket* packet_ = nullptr;
    SwsContext* sws_ctx_ = nullptr;

    std::vector<uint8_t> rgb_buffer_; // rgb_frame_ が使うバックエンドバッファ

    int video_stream_index_ = -1;
    int width_ = 0;
    int height_ = 0;
    double fps_ = 0.0;
    double time_base_ = 0.0; // ストリームのtime_baseを秒に換算した係数
};

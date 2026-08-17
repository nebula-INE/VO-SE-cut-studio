#pragma once

#include <cstdint>
#include <string>

extern "C" {
struct AVFormatContext;
struct AVCodecContext;
struct AVStream;
struct AVFrame;
struct AVPacket;
struct SwsContext;
}

// RGB24フレームを受け取り、H.264にエンコードしてファイル(mp4等)へ
// 書き出すクラス。VideoDecoderの逆方向にあたる。
//
// 使い方:
//   VideoEncoder enc;
//   enc.open("output.mp4", 1280, 720, 30.0);
//   for (each frame) enc.write_frame(rgb_data, 1280, 720);
//   enc.close(); // 必ず呼ぶこと（trailerの書き込みに必要）
class VideoEncoder {
public:
    VideoEncoder();
    ~VideoEncoder();

    VideoEncoder(const VideoEncoder&) = delete;
    VideoEncoder& operator=(const VideoEncoder&) = delete;

    // 出力ファイルを開き、H.264エンコーダとmuxerを初期化する。
    // fps はコンテナ/エンコーダのtime_base決定に使う。
    // bitrate は概算値(bps)。0を渡すとデフォルト値(4Mbps)を使う。
    bool open(const std::string& filename, int width, int height, double fps, int64_t bitrate = 0);

    // RGB24（1ピクセルあたり3バイト、パディング無し）の1フレームを
    // エンコードして書き出す。width/heightはopen()時と一致している必要がある。
    bool write_frame(const uint8_t* rgb_data, int width, int height);

    // エンコーダ内に溜まっているフレームをフラッシュし、trailerを書き込み、
    // ファイルを閉じる。書き出しを完了させるには必須。
    bool close();

    bool is_open() const { return fmt_ctx_ != nullptr; }

private:
    bool encode_and_write(AVFrame* frame); // frame==nullptrでフラッシュ

    AVFormatContext* fmt_ctx_ = nullptr;
    AVCodecContext* codec_ctx_ = nullptr;
    AVStream* stream_ = nullptr;
    AVFrame* yuv_frame_ = nullptr; // エンコーダに渡すYUV420Pフレーム
    AVPacket* packet_ = nullptr;
    SwsContext* sws_ctx_ = nullptr; // RGB24 -> YUV420P

    int width_ = 0;
    int height_ = 0;
    double fps_ = 0.0;
    int64_t frame_index_ = 0; // 送出したフレーム数(=次のpts)
};

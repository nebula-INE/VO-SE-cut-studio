#include "video_encoder.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

#include <iostream>

VideoEncoder::VideoEncoder() = default;

VideoEncoder::~VideoEncoder() {
    if (is_open()) {
        close(); // 呼び忘れてもファイルが壊れたまま残らないようにする
    }
}

bool VideoEncoder::open(const std::string& filename, int width, int height, double fps, int64_t bitrate) {
    if (width <= 0 || height <= 0 || fps <= 0.0) {
        std::cerr << "[VideoEncoder] Invalid parameters (width/height/fps)" << std::endl;
        return false;
    }

    width_ = width;
    height_ = height;
    fps_ = fps;
    frame_index_ = 0;

    // 出力ファイル名の拡張子からコンテナ形式を推測させる(mp4想定)
    int ret = avformat_alloc_output_context2(&fmt_ctx_, nullptr, nullptr, filename.c_str());
    if (!fmt_ctx_ || ret < 0) {
        std::cerr << "[VideoEncoder] Could not create output context for: " << filename << std::endl;
        return false;
    }

    const AVCodec* codec = avcodec_find_encoder_by_name("libx264");
    if (!codec) {
        // libx264が使えない環境向けのフォールバック
        codec = avcodec_find_encoder(AV_CODEC_ID_H264);
    }
    if (!codec) {
        std::cerr << "[VideoEncoder] H.264 encoder not available" << std::endl;
        return false;
    }

    stream_ = avformat_new_stream(fmt_ctx_, nullptr);
    if (!stream_) {
        std::cerr << "[VideoEncoder] Could not create output stream" << std::endl;
        return false;
    }

    codec_ctx_ = avcodec_alloc_context3(codec);
    if (!codec_ctx_) {
        std::cerr << "[VideoEncoder] Could not allocate codec context" << std::endl;
        return false;
    }

    codec_ctx_->width = width_;
    codec_ctx_->height = height_;
    codec_ctx_->pix_fmt = AV_PIX_FMT_YUV420P;
    // time_baseはfpsの逆数（例: 30fps -> 1/30秒刻み）
    codec_ctx_->time_base = AVRational{ static_cast<int>(1000), static_cast<int>(fps_ * 1000) };
    codec_ctx_->framerate = AVRational{ static_cast<int>(fps_ * 1000), 1000 };
    codec_ctx_->bit_rate = (bitrate > 0) ? bitrate : 4'000'000; // デフォルト4Mbps
    codec_ctx_->gop_size = static_cast<int>(fps_); // 約1秒に1キーフレーム

    if (codec->id == AV_CODEC_ID_H264) {
        // 汎用プレイヤー/エディタでの取り回しやすさを優先し、エンコード速度より
        // 互換性・安定性を優先するプリセットにしておく。
        av_opt_set(codec_ctx_->priv_data, "preset", "medium", 0);
        av_opt_set(codec_ctx_->priv_data, "crf", "23", 0);
    }

    // 一部コンテナ(mp4等)はグローバルヘッダーを要求する
    if (fmt_ctx_->oformat->flags & AVFMT_GLOBALHEADER) {
        codec_ctx_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    }

    if (avcodec_open2(codec_ctx_, codec, nullptr) < 0) {
        std::cerr << "[VideoEncoder] Could not open H.264 encoder" << std::endl;
        return false;
    }

    if (avcodec_parameters_from_context(stream_->codecpar, codec_ctx_) < 0) {
        std::cerr << "[VideoEncoder] Could not copy codec parameters to stream" << std::endl;
        return false;
    }
    stream_->time_base = codec_ctx_->time_base;

    // 出力先ファイルを開く(一部フォーマットはメモリ上で完結するのでAVFMT_NOFILEを見て分岐)
    if (!(fmt_ctx_->oformat->flags & AVFMT_NOFILE)) {
        if (avio_open(&fmt_ctx_->pb, filename.c_str(), AVIO_FLAG_WRITE) < 0) {
            std::cerr << "[VideoEncoder] Could not open output file: " << filename << std::endl;
            return false;
        }
    }

    if (avformat_write_header(fmt_ctx_, nullptr) < 0) {
        std::cerr << "[VideoEncoder] Could not write header" << std::endl;
        return false;
    }

    yuv_frame_ = av_frame_alloc();
    packet_ = av_packet_alloc();
    if (!yuv_frame_ || !packet_) {
        std::cerr << "[VideoEncoder] Could not allocate frame/packet" << std::endl;
        return false;
    }

    yuv_frame_->format = AV_PIX_FMT_YUV420P;
    yuv_frame_->width = width_;
    yuv_frame_->height = height_;
    if (av_frame_get_buffer(yuv_frame_, 32) < 0) {
        std::cerr << "[VideoEncoder] Could not allocate frame buffer" << std::endl;
        return false;
    }

    sws_ctx_ = sws_getContext(
        width_, height_, AV_PIX_FMT_RGB24,
        width_, height_, AV_PIX_FMT_YUV420P,
        SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (!sws_ctx_) {
        std::cerr << "[VideoEncoder] Could not initialize sws context" << std::endl;
        return false;
    }

    return true;
}

bool VideoEncoder::write_frame(const uint8_t* rgb_data, int width, int height) {
    if (!is_open()) {
        std::cerr << "[VideoEncoder] write_frame() called before open()" << std::endl;
        return false;
    }
    if (width != width_ || height != height_) {
        std::cerr << "[VideoEncoder] Frame size mismatch: expected "
                  << width_ << "x" << height_ << ", got " << width << "x" << height << std::endl;
        return false;
    }

    if (av_frame_make_writable(yuv_frame_) < 0) {
        std::cerr << "[VideoEncoder] Frame not writable" << std::endl;
        return false;
    }

    // RGB24の入力は3バイト/pixel、パディング無し前提でlinesizeを算出
    const uint8_t* src_data[1] = { rgb_data };
    int src_linesize[1] = { width_ * 3 };

    sws_scale(
        sws_ctx_,
        src_data, src_linesize,
        0, height_,
        yuv_frame_->data, yuv_frame_->linesize);

    yuv_frame_->pts = frame_index_++;

    return encode_and_write(yuv_frame_);
}

bool VideoEncoder::encode_and_write(AVFrame* frame) {
    int send_ret = avcodec_send_frame(codec_ctx_, frame);
    if (send_ret < 0) {
        std::cerr << "[VideoEncoder] Error sending frame to encoder" << std::endl;
        return false;
    }

    while (true) {
        int recv_ret = avcodec_receive_packet(codec_ctx_, packet_);
        if (recv_ret == AVERROR(EAGAIN) || recv_ret == AVERROR_EOF) {
            break; // このフレームからはこれ以上パケットが出てこない
        } else if (recv_ret < 0) {
            std::cerr << "[VideoEncoder] Error receiving packet from encoder" << std::endl;
            return false;
        }

        // エンコーダのtime_base -> streamのtime_base へ変換
        av_packet_rescale_ts(packet_, codec_ctx_->time_base, stream_->time_base);
        packet_->stream_index = stream_->index;

        if (av_interleaved_write_frame(fmt_ctx_, packet_) < 0) {
            std::cerr << "[VideoEncoder] Error writing packet" << std::endl;
            av_packet_unref(packet_);
            return false;
        }
        // av_interleaved_write_frame が packet_ の所有権を引き取るので unref 不要
    }

    return true;
}

bool VideoEncoder::close() {
    if (!fmt_ctx_) {
        return true; // 既に閉じている/開いていない
    }

    bool ok = true;

    if (codec_ctx_) {
        // エンコーダ内部にバッファされているフレームを全て吐き出させる
        ok = encode_and_write(nullptr) && ok;
    }

    if (fmt_ctx_->pb) {
        if (av_write_trailer(fmt_ctx_) < 0) {
            std::cerr << "[VideoEncoder] Could not write trailer" << std::endl;
            ok = false;
        }
    }

    if (sws_ctx_) {
        sws_freeContext(sws_ctx_);
        sws_ctx_ = nullptr;
    }
    if (yuv_frame_) {
        av_frame_free(&yuv_frame_);
    }
    if (packet_) {
        av_packet_free(&packet_);
    }
    if (codec_ctx_) {
        avcodec_free_context(&codec_ctx_);
    }
    if (fmt_ctx_) {
        if (!(fmt_ctx_->oformat->flags & AVFMT_NOFILE) && fmt_ctx_->pb) {
            avio_closep(&fmt_ctx_->pb);
        }
        avformat_free_context(fmt_ctx_);
        fmt_ctx_ = nullptr;
    }

    stream_ = nullptr;
    return ok;
}

#include "video_decoder.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
}

#include <iostream>

VideoDecoder::VideoDecoder() = default;

VideoDecoder::~VideoDecoder() {
    close();
}

bool VideoDecoder::open(const std::string& filename) {
    close(); // 念のため既存リソースを解放しておく

    if (avformat_open_input(&fmt_ctx_, filename.c_str(), nullptr, nullptr) < 0) {
        std::cerr << "[VideoDecoder] Could not open file: " << filename << std::endl;
        return false;
    }

    if (avformat_find_stream_info(fmt_ctx_, nullptr) < 0) {
        std::cerr << "[VideoDecoder] Could not find stream info" << std::endl;
        close();
        return false;
    }

    // 最初のビデオストリームを探す
    video_stream_index_ = -1;
    for (unsigned int i = 0; i < fmt_ctx_->nb_streams; i++) {
        if (fmt_ctx_->streams[i]->codecpar->codec_type == AVMEDIA_TYPE_VIDEO) {
            video_stream_index_ = static_cast<int>(i);
            break;
        }
    }

    if (video_stream_index_ == -1) {
        std::cerr << "[VideoDecoder] No video stream found in: " << filename << std::endl;
        close();
        return false;
    }

    AVStream* stream = fmt_ctx_->streams[video_stream_index_];
    AVCodecParameters* codecpar = stream->codecpar;

    const AVCodec* codec = avcodec_find_decoder(codecpar->codec_id);
    if (!codec) {
        std::cerr << "[VideoDecoder] Unsupported codec: " << avcodec_get_name(codecpar->codec_id) << std::endl;
        close();
        return false;
    }

    codec_ctx_ = avcodec_alloc_context3(codec);
    if (!codec_ctx_) {
        std::cerr << "[VideoDecoder] Could not allocate codec context" << std::endl;
        close();
        return false;
    }

    if (avcodec_parameters_to_context(codec_ctx_, codecpar) < 0) {
        std::cerr << "[VideoDecoder] Could not copy codec parameters" << std::endl;
        close();
        return false;
    }

    if (avcodec_open2(codec_ctx_, codec, nullptr) < 0) {
        std::cerr << "[VideoDecoder] Could not open codec" << std::endl;
        close();
        return false;
    }

    width_ = codec_ctx_->width;
    height_ = codec_ctx_->height;

    // フレームレート算出（avg_frame_rateが無効な場合はr_frame_rateにフォールバック）
    AVRational fr = stream->avg_frame_rate;
    if (fr.num == 0 || fr.den == 0) {
        fr = stream->r_frame_rate;
    }
    fps_ = (fr.den != 0) ? (static_cast<double>(fr.num) / fr.den) : 0.0;

    // time_base を秒への変換係数として保持
    time_base_ = av_q2d(stream->time_base);

    frame_ = av_frame_alloc();
    rgb_frame_ = av_frame_alloc();
    packet_ = av_packet_alloc();

    if (!frame_ || !rgb_frame_ || !packet_) {
        std::cerr << "[VideoDecoder] Could not allocate frame/packet" << std::endl;
        close();
        return false;
    }

    if (!init_sws_context()) {
        close();
        return false;
    }

    return true;
}

bool VideoDecoder::init_sws_context() {
    sws_ctx_ = sws_getContext(
        width_, height_, codec_ctx_->pix_fmt,
        width_, height_, AV_PIX_FMT_RGB24,
        SWS_BILINEAR, nullptr, nullptr, nullptr);

    if (!sws_ctx_) {
        std::cerr << "[VideoDecoder] Could not initialize sws context" << std::endl;
        return false;
    }

    // RGB24用の出力バッファを確保し、rgb_frame_ にリンクする
    int num_bytes = av_image_get_buffer_size(AV_PIX_FMT_RGB24, width_, height_, 1);
    rgb_buffer_.resize(static_cast<size_t>(num_bytes));

    av_image_fill_arrays(
        rgb_frame_->data, rgb_frame_->linesize,
        rgb_buffer_.data(), AV_PIX_FMT_RGB24,
        width_, height_, 1);

    return true;
}

bool VideoDecoder::decode_next_frame(DecodedFrame& out) {
    if (!fmt_ctx_ || !codec_ctx_) {
        return false;
    }

    while (true) {
        int read_ret = av_read_frame(fmt_ctx_, packet_);
        if (read_ret < 0) {
            // ファイル終端。デコーダ内部に残っているフレームをフラッシュする。
            avcodec_send_packet(codec_ctx_, nullptr);
            int recv_ret = avcodec_receive_frame(codec_ctx_, frame_);
            if (recv_ret < 0) {
                return false; // 本当に終わり
            }
            // フラッシュで取れたフレームを処理して return する
        } else {
            if (packet_->stream_index != video_stream_index_) {
                av_packet_unref(packet_);
                continue; // 音声など、ビデオ以外のパケットは読み飛ばす
            }

            int send_ret = avcodec_send_packet(codec_ctx_, packet_);
            av_packet_unref(packet_);

            if (send_ret < 0) {
                std::cerr << "[VideoDecoder] Error sending packet to decoder" << std::endl;
                return false;
            }

            int recv_ret = avcodec_receive_frame(codec_ctx_, frame_);
            if (recv_ret == AVERROR(EAGAIN)) {
                continue; // まだ1枚分揃っていない。次のパケットを読む。
            } else if (recv_ret < 0) {
                std::cerr << "[VideoDecoder] Error receiving frame from decoder" << std::endl;
                return false;
            }
        }

        // ここまで来れば frame_ に1枚分のデコード結果が入っている
        sws_scale(
            sws_ctx_,
            frame_->data, frame_->linesize,
            0, height_,
            rgb_frame_->data, rgb_frame_->linesize);

        out.width = width_;
        out.height = height_;
        out.rgb_data.assign(rgb_buffer_.begin(), rgb_buffer_.end());

        int64_t pts = frame_->best_effort_timestamp;
        out.pts_seconds = (pts != AV_NOPTS_VALUE) ? (pts * time_base_) : 0.0;

        av_frame_unref(frame_);
        return true;
    }
}

bool VideoDecoder::seek(double seconds) {
    if (!fmt_ctx_ || video_stream_index_ < 0) {
        return false;
    }

    int64_t target_ts = static_cast<int64_t>(seconds / time_base_);

    int ret = av_seek_frame(fmt_ctx_, video_stream_index_, target_ts, AVSEEK_FLAG_BACKWARD);
    if (ret < 0) {
        std::cerr << "[VideoDecoder] Seek failed" << std::endl;
        return false;
    }

    avcodec_flush_buffers(codec_ctx_);
    return true;
}

double VideoDecoder::duration_seconds() const {
    if (!fmt_ctx_ || fmt_ctx_->duration == AV_NOPTS_VALUE) {
        return 0.0;
    }
    return static_cast<double>(fmt_ctx_->duration) / AV_TIME_BASE;
}

void VideoDecoder::close() {
    if (sws_ctx_) {
        sws_freeContext(sws_ctx_);
        sws_ctx_ = nullptr;
    }
    if (packet_) {
        av_packet_free(&packet_);
    }
    if (frame_) {
        av_frame_free(&frame_);
    }
    if (rgb_frame_) {
        av_frame_free(&rgb_frame_);
    }
    if (codec_ctx_) {
        avcodec_free_context(&codec_ctx_);
    }
    if (fmt_ctx_) {
        avformat_close_input(&fmt_ctx_);
    }

    rgb_buffer_.clear();
    video_stream_index_ = -1;
    width_ = height_ = 0;
    fps_ = 0.0;
    time_base_ = 0.0;
}

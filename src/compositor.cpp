#include "compositor.h"

extern "C" {
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libavutil/pixdesc.h>
#include <libswscale/swscale.h>
}

#include <cstdio>
#include <cstring>
#include <iostream>

Compositor::Compositor() = default;

Compositor::~Compositor() {
    close();
}

bool Compositor::init(int bg_width, int bg_height, int ov_width, int ov_height,
                       int overlay_x, int overlay_y) {
    close(); // 念のため既存グラフを解放

    bg_width_ = bg_width;
    bg_height_ = bg_height;
    ov_width_ = ov_width;
    ov_height_ = ov_height;
    pts_counter_ = 0;

    graph_ = avfilter_graph_alloc();
    if (!graph_) {
        std::cerr << "[Compositor] Could not allocate filter graph" << std::endl;
        return false;
    }

    const AVFilter* buffersrc = avfilter_get_by_name("buffer");
    const AVFilter* overlay_filter = avfilter_get_by_name("overlay");
    const AVFilter* buffersink = avfilter_get_by_name("buffersink");

    if (!buffersrc || !overlay_filter || !buffersink) {
        std::cerr << "[Compositor] Required filters not found in this FFmpeg build" << std::endl;
        close();
        return false;
    }

    // --- 背景入力(buffer) --- YUV420Pで宣言し、overlayの自動フォーマット
    // 変換挿入(バグの温床)を避ける。
    char bg_args[256];
    std::snprintf(bg_args, sizeof(bg_args),
        "video_size=%dx%d:pix_fmt=%d:time_base=1/1000:pixel_aspect=1/1",
        bg_width_, bg_height_, static_cast<int>(AV_PIX_FMT_YUV420P));

    if (avfilter_graph_create_filter(&bg_src_ctx_, buffersrc, "bg_in", bg_args, nullptr, graph_) < 0) {
        std::cerr << "[Compositor] Could not create background buffer source" << std::endl;
        close();
        return false;
    }

    // --- オーバーレイ入力(buffer) ---
    char ov_args[256];
    std::snprintf(ov_args, sizeof(ov_args),
        "video_size=%dx%d:pix_fmt=%d:time_base=1/1000:pixel_aspect=1/1",
        ov_width_, ov_height_, static_cast<int>(AV_PIX_FMT_YUV420P));

    if (avfilter_graph_create_filter(&ov_src_ctx_, buffersrc, "ov_in", ov_args, nullptr, graph_) < 0) {
        std::cerr << "[Compositor] Could not create overlay buffer source" << std::endl;
        close();
        return false;
    }

    // --- overlayフィルタ本体 ---
    char overlay_args[64];
    std::snprintf(overlay_args, sizeof(overlay_args), "x=%d:y=%d", overlay_x, overlay_y);

    if (avfilter_graph_create_filter(&overlay_ctx_, overlay_filter, "overlay", overlay_args, nullptr, graph_) < 0) {
        std::cerr << "[Compositor] Could not create overlay filter" << std::endl;
        close();
        return false;
    }

    // --- 出力(buffersink) ---
    if (avfilter_graph_create_filter(&sink_ctx_, buffersink, "out", nullptr, nullptr, graph_) < 0) {
        std::cerr << "[Compositor] Could not create buffer sink" << std::endl;
        close();
        return false;
    }

    // buffersinkの出力フォーマットをYUV420Pに固定
    enum AVPixelFormat pix_fmts[] = { AV_PIX_FMT_YUV420P, AV_PIX_FMT_NONE };
    if (av_opt_set_int_list(sink_ctx_, "pix_fmts", pix_fmts, AV_PIX_FMT_NONE, AV_OPT_SEARCH_CHILDREN) < 0) {
        std::cerr << "[Compositor] Could not set output pixel format" << std::endl;
        close();
        return false;
    }

    // --- 接続: bg -> overlay(pad 0), ov -> overlay(pad 1), overlay -> sink ---
    if (avfilter_link(bg_src_ctx_, 0, overlay_ctx_, 0) < 0 ||
        avfilter_link(ov_src_ctx_, 0, overlay_ctx_, 1) < 0 ||
        avfilter_link(overlay_ctx_, 0, sink_ctx_, 0) < 0) {
        std::cerr << "[Compositor] Could not link filter graph" << std::endl;
        close();
        return false;
    }

    if (avfilter_graph_config(graph_, nullptr) < 0) {
        std::cerr << "[Compositor] Could not configure filter graph" << std::endl;
        close();
        return false;
    }

    bg_yuv_frame_ = av_frame_alloc();
    ov_yuv_frame_ = av_frame_alloc();
    out_yuv_frame_ = av_frame_alloc();
    if (!bg_yuv_frame_ || !ov_yuv_frame_ || !out_yuv_frame_) {
        std::cerr << "[Compositor] Could not allocate frames" << std::endl;
        close();
        return false;
    }

    // RGB24 <-> YUV420P 変換用のsws context群
    bg_rgb_to_yuv_ = sws_getContext(
        bg_width_, bg_height_, AV_PIX_FMT_RGB24,
        bg_width_, bg_height_, AV_PIX_FMT_YUV420P,
        SWS_BILINEAR, nullptr, nullptr, nullptr);
    ov_rgb_to_yuv_ = sws_getContext(
        ov_width_, ov_height_, AV_PIX_FMT_RGB24,
        ov_width_, ov_height_, AV_PIX_FMT_YUV420P,
        SWS_BILINEAR, nullptr, nullptr, nullptr);
    out_yuv_to_rgb_ = sws_getContext(
        bg_width_, bg_height_, AV_PIX_FMT_YUV420P,
        bg_width_, bg_height_, AV_PIX_FMT_RGB24,
        SWS_BILINEAR, nullptr, nullptr, nullptr);

    if (!bg_rgb_to_yuv_ || !ov_rgb_to_yuv_ || !out_yuv_to_rgb_) {
        std::cerr << "[Compositor] Could not initialize sws contexts" << std::endl;
        close();
        return false;
    }

    return true;
}

// RGB24の生バッファをsws_scaleでYUV420Pへ変換し、frameへ詰める共通ヘルパ
static bool rgb_to_yuv_frame(SwsContext* sws, AVFrame* frame,
                              const uint8_t* rgb_data, int width, int height, int64_t pts) {
    av_frame_unref(frame);
    frame->format = AV_PIX_FMT_YUV420P;
    frame->width = width;
    frame->height = height;
    frame->pts = pts;

    if (av_frame_get_buffer(frame, 32) < 0) {
        std::cerr << "[Compositor] Could not allocate frame buffer" << std::endl;
        return false;
    }
    if (av_frame_make_writable(frame) < 0) {
        return false;
    }

    const uint8_t* src_data[1] = { rgb_data };
    int src_linesize[1] = { width * 3 };

    sws_scale(sws, src_data, src_linesize, 0, height, frame->data, frame->linesize);
    return true;
}

bool Compositor::process(const uint8_t* bg_rgb, const uint8_t* ov_rgb, std::vector<uint8_t>& out_rgb) {
    if (!is_initialized()) {
        std::cerr << "[Compositor] process() called before init()" << std::endl;
        return false;
    }

    int64_t pts = pts_counter_++;

    if (!rgb_to_yuv_frame(bg_rgb_to_yuv_, bg_yuv_frame_, bg_rgb, bg_width_, bg_height_, pts)) {
        return false;
    }
    if (!rgb_to_yuv_frame(ov_rgb_to_yuv_, ov_yuv_frame_, ov_rgb, ov_width_, ov_height_, pts)) {
        return false;
    }

    if (av_buffersrc_add_frame_flags(bg_src_ctx_, bg_yuv_frame_, AV_BUFFERSRC_FLAG_KEEP_REF) < 0) {
        std::cerr << "[Compositor] Could not feed background frame into graph" << std::endl;
        return false;
    }
    if (av_buffersrc_add_frame_flags(ov_src_ctx_, ov_yuv_frame_, AV_BUFFERSRC_FLAG_KEEP_REF) < 0) {
        std::cerr << "[Compositor] Could not feed overlay frame into graph" << std::endl;
        return false;
    }

    av_frame_unref(out_yuv_frame_);
    int ret = av_buffersink_get_frame(sink_ctx_, out_yuv_frame_);
    if (ret < 0) {
        std::cerr << "[Compositor] Could not pull composited frame from graph" << std::endl;
        return false;
    }

    // YUV420P(合成結果) -> RGB24(出力バッファ)
    // sws_scaleのSIMD最適化ルーチンが末尾を数バイト超えて書き込むことがあるため、
    // VideoDecoderと同様に安全マージンを確保してから論理サイズへresizeする。
    out_rgb.reserve(static_cast<size_t>(bg_width_) * bg_height_ * 3 + 64);
    out_rgb.resize(static_cast<size_t>(bg_width_) * bg_height_ * 3);
    uint8_t* dst_data[1] = { out_rgb.data() };
    int dst_linesize[1] = { bg_width_ * 3 };

    sws_scale(out_yuv_to_rgb_, out_yuv_frame_->data, out_yuv_frame_->linesize,
              0, bg_height_, dst_data, dst_linesize);

    av_frame_unref(out_yuv_frame_);
    return true;
}

void Compositor::close() {
    if (bg_yuv_frame_) av_frame_free(&bg_yuv_frame_);
    if (ov_yuv_frame_) av_frame_free(&ov_yuv_frame_);
    if (out_yuv_frame_) av_frame_free(&out_yuv_frame_);

    if (bg_rgb_to_yuv_) { sws_freeContext(bg_rgb_to_yuv_); bg_rgb_to_yuv_ = nullptr; }
    if (ov_rgb_to_yuv_) { sws_freeContext(ov_rgb_to_yuv_); ov_rgb_to_yuv_ = nullptr; }
    if (out_yuv_to_rgb_) { sws_freeContext(out_yuv_to_rgb_); out_yuv_to_rgb_ = nullptr; }

    if (graph_) {
        avfilter_graph_free(&graph_); // 内部のフィルタコンテキストも全てまとめて解放される
        graph_ = nullptr;
    }
    bg_src_ctx_ = ov_src_ctx_ = overlay_ctx_ = sink_ctx_ = nullptr;
}

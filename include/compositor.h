#pragma once

#include <cstdint>
#include <vector>

extern "C" {
struct AVFilterGraph;
struct AVFilterContext;
struct AVFrame;
struct SwsContext;
}

// 背景映像フレームの上にオーバーレイ映像フレーム(キャラクター等)を
// libavfilterの"overlay"フィルタで重ね合わせるクラス。
//
// 外部インターフェースはRGB24（VideoDecoder/VideoEncoderと同じ形式）。
// 内部ではYUV420Pでoverlayフィルタを通す。RGB24をそのままoverlayに
// 渡すと、libavfilterが自動挿入するフォーマット変換フィルタの後始末で
// 一部の環境/バージョンで解放時にクラッシュする問題があったため、
// overlayフィルタが最も安定して対応しているYUV420Pに揃えて
// 自動変換の挿入自体を避けている。
//
// 使い方:
//   Compositor comp;
//   comp.init(bg_w, bg_h, ov_w, ov_h, overlay_x, overlay_y);
//   std::vector<uint8_t> composited;
//   comp.process(bg_rgb, ov_rgb, composited);
class Compositor {
public:
    Compositor();
    ~Compositor();

    Compositor(const Compositor&) = delete;
    Compositor& operator=(const Compositor&) = delete;

    // bg_width/bg_height: 背景(=出力)の解像度
    // ov_width/ov_height: オーバーレイ映像の解像度
    // overlay_x/overlay_y: 背景上でのオーバーレイ左上座標(ピクセル)
    bool init(int bg_width, int bg_height, int ov_width, int ov_height,
              int overlay_x, int overlay_y);

    // bg_rgb: 背景フレーム(bg_width*bg_height*3 バイト, RGB24)
    // ov_rgb: オーバーレイフレーム(ov_width*ov_height*3 バイト, RGB24)
    // out_rgb: 合成結果(bg_width*bg_height*3 バイト, RGB24)を書き込む先
    // 成功時 true。
    bool process(const uint8_t* bg_rgb, const uint8_t* ov_rgb, std::vector<uint8_t>& out_rgb);

    void close();

    bool is_initialized() const { return graph_ != nullptr; }

private:
    AVFilterGraph* graph_ = nullptr;
    AVFilterContext* bg_src_ctx_ = nullptr;
    AVFilterContext* ov_src_ctx_ = nullptr;
    AVFilterContext* overlay_ctx_ = nullptr;
    AVFilterContext* sink_ctx_ = nullptr;

    AVFrame* bg_yuv_frame_ = nullptr; // bg_rgb -> YUV420P 変換後、グラフへ投入
    AVFrame* ov_yuv_frame_ = nullptr; // ov_rgb -> YUV420P 変換後、グラフへ投入
    AVFrame* out_yuv_frame_ = nullptr; // グラフから取り出したYUV420P出力

    SwsContext* bg_rgb_to_yuv_ = nullptr;
    SwsContext* ov_rgb_to_yuv_ = nullptr;
    SwsContext* out_yuv_to_rgb_ = nullptr;

    int bg_width_ = 0;
    int bg_height_ = 0;
    int ov_width_ = 0;
    int ov_height_ = 0;

    int64_t pts_counter_ = 0; // 両方の入力に共通で使う擬似PTS
};

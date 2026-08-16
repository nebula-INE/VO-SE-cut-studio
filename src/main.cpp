#include "compositor.h"
#include "video_decoder.h"
#include "video_encoder.h"

#include <chrono>
#include <iostream>

// 使い方: aural-compose <background> <overlay> <output.mp4> [overlay_x] [overlay_y]
//
// 背景動画とオーバーレイ動画(キャラクター等)を2本デコードし、
// libavfilterのoverlayで合成した上でH.264として書き出す。
// 出力の解像度・フレームレートは背景側に合わせる。
// オーバーレイ側が背景より短い場合は、最後のフレームを静止させたまま
// 背景の終端まで合成を続ける。
int main(int argc, char* argv[]) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0]
                  << " <background> <overlay> <output.mp4> [overlay_x] [overlay_y]\n";
        return 1;
    }

    const std::string bg_path = argv[1];
    const std::string ov_path = argv[2];
    const std::string out_path = argv[3];
    const int overlay_x = (argc > 4) ? std::stoi(argv[4]) : 0;
    const int overlay_y = (argc > 5) ? std::stoi(argv[5]) : 0;

    VideoDecoder bg_decoder;
    if (!bg_decoder.open(bg_path)) {
        std::cerr << "Failed to open background: " << bg_path << std::endl;
        return 1;
    }

    VideoDecoder ov_decoder;
    if (!ov_decoder.open(ov_path)) {
        std::cerr << "Failed to open overlay: " << ov_path << std::endl;
        return 1;
    }

    std::cout << "Background: " << bg_decoder.width() << "x" << bg_decoder.height()
              << " @ " << bg_decoder.fps() << "fps, " << bg_decoder.duration_seconds() << "s\n";
    std::cout << "Overlay:    " << ov_decoder.width() << "x" << ov_decoder.height()
              << " @ " << ov_decoder.fps() << "fps, " << ov_decoder.duration_seconds() << "s\n";
    std::cout << "Overlay position: (" << overlay_x << ", " << overlay_y << ")\n";

    Compositor compositor;
    if (!compositor.init(bg_decoder.width(), bg_decoder.height(),
                          ov_decoder.width(), ov_decoder.height(),
                          overlay_x, overlay_y)) {
        std::cerr << "Failed to initialize compositor" << std::endl;
        return 1;
    }

    VideoEncoder encoder;
    if (!encoder.open(out_path, bg_decoder.width(), bg_decoder.height(), bg_decoder.fps())) {
        std::cerr << "Failed to open output: " << out_path << std::endl;
        return 1;
    }

    DecodedFrame bg_frame;
    DecodedFrame ov_frame;
    std::vector<uint8_t> composited;

    bool ov_has_frame = ov_decoder.decode_next_frame(ov_frame); // 最初のオーバーレイフレーム
    int frame_count = 0;

    auto start = std::chrono::steady_clock::now();

    while (bg_decoder.decode_next_frame(bg_frame)) {
        // オーバーレイが背景より短い場合は最後のフレームで静止させ続ける。
        // (decode_next_frameがfalseを返した後もov_frameは直前の内容を保持している)
        if (ov_has_frame) {
            ov_has_frame = ov_decoder.decode_next_frame(ov_frame);
        }

        if (!compositor.process(bg_frame.rgb_data.data(), ov_frame.rgb_data.data(), composited)) {
            std::cerr << "Compositing failed at frame " << frame_count << std::endl;
            break;
        }

        if (!encoder.write_frame(composited.data(), bg_decoder.width(), bg_decoder.height())) {
            std::cerr << "Encoding failed at frame " << frame_count << std::endl;
            break;
        }

        frame_count++;
        if (frame_count % 30 == 0) {
            std::cout << "Processed " << frame_count << " frames (pts="
                      << bg_frame.pts_seconds << "s)" << std::endl;
        }
    }

    encoder.close();

    auto end = std::chrono::steady_clock::now();
    double elapsed = std::chrono::duration<double>(end - start).count();

    std::cout << "\nDone. Composited & encoded " << frame_count << " frames in "
              << elapsed << "s -> " << out_path << std::endl;

    return 0;
}

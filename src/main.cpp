#include "video_decoder.h"

#include <chrono>
#include <iostream>

// 現段階ではまだGUI(ウィンドウ/GLコンテキスト)が無いため、
// TextureUploaderの実動作確認はGUI実装フェーズで行う。
// ここではデコーダ単体が正しく・十分な速度で動くかを確認する
// ヘッドレスなテストドライバとして動作する。
int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <video_file>\n";
        return 1;
    }

    const char* filename = argv[1];

    VideoDecoder decoder;
    if (!decoder.open(filename)) {
        std::cerr << "Failed to open: " << filename << std::endl;
        return 1;
    }

    std::cout << "File opened successfully!\n";
    std::cout << "Resolution: " << decoder.width() << "x" << decoder.height() << std::endl;
    std::cout << "FPS: " << decoder.fps() << std::endl;
    std::cout << "Duration: " << decoder.duration_seconds() << "s" << std::endl;

    DecodedFrame frame;
    int frame_count = 0;

    auto start = std::chrono::steady_clock::now();

    while (decoder.decode_next_frame(frame)) {
        frame_count++;

        // 10フレームごとに進捗を出す（ログで埋め尽くさないため）
        if (frame_count % 10 == 0) {
            std::cout << "Decoded frame " << frame_count
                      << " (pts=" << frame.pts_seconds << "s, "
                      << frame.rgb_data.size() << " bytes)" << std::endl;
        }
    }

    auto end = std::chrono::steady_clock::now();
    double elapsed_sec = std::chrono::duration<double>(end - start).count();
    double decode_fps = (elapsed_sec > 0.0) ? (frame_count / elapsed_sec) : 0.0;

    std::cout << "\nDone. Decoded " << frame_count << " frames in "
              << elapsed_sec << "s (" << decode_fps << " fps decode speed)." << std::endl;

    return 0;
}

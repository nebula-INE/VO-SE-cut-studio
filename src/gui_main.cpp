#include "texture_uploader.h"
#include "video_decoder.h"

#include <GLFW/glfw3.h>

#include <chrono>
#include <cstring>
#include <iostream>
#include <thread>

// 使い方: aural-gui <video_file> [--dump-frame <output.png>]
//
// GLFWでウィンドウ/OpenGLコンテキストを作成し、VideoDecoderでデコードした
// フレームをTextureUploaderでテクスチャ化、ウィンドウいっぱいの矩形に
// テクスチャを貼って毎フレーム描画する、最小限のプレビュープレイヤー。
// 動画の終端に達したら先頭にシークしてループ再生を続ける。
//
// --dump-frame は自動テスト/CI向けのデバッグオプション。指定すると、
// 最初の1フレームを描画した直後にPNG(実体はPPM->変換なしの生ダンプでは
// なく、glReadPixelsした結果をそのままPPMとして書き出す)として保存し、
// 終了する。ディスプレイの無い環境(Xvfb等)でも描画結果を検証できる。

namespace {

void glfw_error_callback(int error, const char* description) {
    std::cerr << "[GLFW error " << error << "] " << description << std::endl;
}

void framebuffer_size_callback(GLFWwindow* /*window*/, int width, int height) {
    glViewport(0, 0, width, height);
}

// テクスチャをウィンドウいっぱいの矩形に描画する(固定機能パイプライン、GL1.1相当)
void draw_fullscreen_quad(uint32_t texture_id) {
    glEnable(GL_TEXTURE_2D);
    glBindTexture(GL_TEXTURE_2D, texture_id);

    glBegin(GL_QUADS);
        // OpenGLのテクスチャ座標系は左下が原点(V=0)なのに対し、
        // デコードしたフレームは上から下に走査した並びなので、
        // V座標を反転させて上下が正しくなるようにする。
        glTexCoord2f(0.0f, 1.0f); glVertex2f(-1.0f, -1.0f);
        glTexCoord2f(1.0f, 1.0f); glVertex2f( 1.0f, -1.0f);
        glTexCoord2f(1.0f, 0.0f); glVertex2f( 1.0f,  1.0f);
        glTexCoord2f(0.0f, 0.0f); glVertex2f(-1.0f,  1.0f);
    glEnd();

    glBindTexture(GL_TEXTURE_2D, 0);
    glDisable(GL_TEXTURE_2D);
}

// 現在のフレームバッファの内容をPPM(P6)として保存する。
// PNGエンコーダを新規に持ち込まずに済む、テスト用の最小限のダンプ手段。
bool dump_framebuffer_ppm(const std::string& path, int width, int height) {
    std::vector<uint8_t> pixels(static_cast<size_t>(width) * height * 3);
    glReadPixels(0, 0, width, height, GL_RGB, GL_UNSIGNED_BYTE, pixels.data());

    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) {
        std::cerr << "[aural-gui] Could not open dump path: " << path << std::endl;
        return false;
    }
    std::fprintf(f, "P6\n%d %d\n255\n", width, height);
    // OpenGLはフレームバッファを左下原点で返すため、PPM書き出し時に
    // 上下反転させる(画像フォーマットは通常、左上原点で行を並べるため)。
    for (int y = height - 1; y >= 0; y--) {
        std::fwrite(pixels.data() + static_cast<size_t>(y) * width * 3, 1, static_cast<size_t>(width) * 3, f);
    }
    std::fclose(f);
    return true;
}

} // namespace

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <video_file> [--dump-frame <output.ppm>]\n";
        return 1;
    }

    const std::string video_path = argv[1];
    std::string dump_frame_path;
    for (int i = 2; i < argc; i++) {
        if (std::strcmp(argv[i], "--dump-frame") == 0 && i + 1 < argc) {
            dump_frame_path = argv[i + 1];
            i++;
        }
    }

    VideoDecoder decoder;
    if (!decoder.open(video_path)) {
        std::cerr << "Failed to open video: " << video_path << std::endl;
        return 1;
    }

    std::cout << "Loaded: " << decoder.width() << "x" << decoder.height()
              << " @ " << decoder.fps() << "fps" << std::endl;

    glfwSetErrorCallback(glfw_error_callback);

    if (!glfwInit()) {
        std::cerr << "[aural-gui] glfwInit failed" << std::endl;
        return 1;
    }

    // 固定機能パイプライン(glBegin/glEnd)を使うため、プロファイル/バージョンの
    // ヒントは指定しない(GLFW_OPENGL_PROFILEはGL3.2以降でしか意味を持たず、
    // 指定するとエラーになる)。デフォルトで互換コンテキストが得られる。

    GLFWwindow* window = glfwCreateWindow(decoder.width(), decoder.height(), "aural preview", nullptr, nullptr);
    if (!window) {
        std::cerr << "[aural-gui] glfwCreateWindow failed" << std::endl;
        glfwTerminate();
        return 1;
    }

    glfwMakeContextCurrent(window);
    glfwSetFramebufferSizeCallback(window, framebuffer_size_callback);
    glfwSwapInterval(1); // vsync有効化。fps制御の主目的はデコード側のタイミング調整。

    TextureUploader uploader;
    if (!uploader.init(decoder.width(), decoder.height())) {
        std::cerr << "[aural-gui] TextureUploader init failed" << std::endl;
        glfwTerminate();
        return 1;
    }

    const double frame_interval = (decoder.fps() > 0.0) ? (1.0 / decoder.fps()) : (1.0 / 30.0);

    DecodedFrame frame;
    bool have_frame = decoder.decode_next_frame(frame);

    // 最初のフレームはタイミング待ちせず即座にアップロードしておく。
    // これをしないと、ループ1周目は elapsed < frame_interval となり、
    // テクスチャが未初期化(真っ黒)のまま描画されてしまう。
    if (have_frame) {
        uploader.upload(frame.rgb_data.data(), frame.width, frame.height);
        have_frame = decoder.decode_next_frame(frame);
    }
    auto last_frame_time = std::chrono::steady_clock::now();

    bool dumped = false;

    while (!glfwWindowShouldClose(window)) {
        auto now = std::chrono::steady_clock::now();
        double elapsed = std::chrono::duration<double>(now - last_frame_time).count();

        if (have_frame && elapsed >= frame_interval) {
            uploader.upload(frame.rgb_data.data(), frame.width, frame.height);
            last_frame_time = now;

            have_frame = decoder.decode_next_frame(frame);
            if (!have_frame) {
                // 動画の終端に達した。先頭にシークしてループ再生する。
                if (decoder.seek(0.0)) {
                    have_frame = decoder.decode_next_frame(frame);
                }
            }
        }

        int fb_width, fb_height;
        glfwGetFramebufferSize(window, &fb_width, &fb_height);
        glViewport(0, 0, fb_width, fb_height);
        glClearColor(0.1f, 0.1f, 0.1f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);

        draw_fullscreen_quad(uploader.texture_id());

        if (!dump_frame_path.empty() && !dumped) {
            dump_framebuffer_ppm(dump_frame_path, fb_width, fb_height);
            dumped = true;
            glfwSwapBuffers(window);
            break; // テスト用ダンプが目的の起動なので、1フレーム描画したら終了する
        }

        glfwSwapBuffers(window);
        glfwPollEvents();

        // vsyncが効かない環境(オフスクリーン等)向けに、CPU使用率を
        // 抑えるための簡易スリープ。
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }

    glfwDestroyWindow(window);
    glfwTerminate();
    return 0;
}

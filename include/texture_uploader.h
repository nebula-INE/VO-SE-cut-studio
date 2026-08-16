#pragma once

#include <cstdint>

// デコード済みRGBフレームをOpenGLテクスチャへアップロードするクラス。
// GUI側(将来のPhase)では、このクラスが保持するテクスチャIDをそのまま
// ImGui/GLFW等の描画パイプラインに渡して画面に表示する想定。
//
// 使い方:
//   TextureUploader uploader;
//   uploader.init(width, height);
//   ...decode loop...
//   uploader.upload(frame.rgb_data.data(), frame.width, frame.height);
//   GLuint tex_id = uploader.texture_id();
class TextureUploader {
public:
    TextureUploader();
    ~TextureUploader();

    TextureUploader(const TextureUploader&) = delete;
    TextureUploader& operator=(const TextureUploader&) = delete;

    // 指定サイズのテクスチャを確保する。有効なOpenGLコンテキストが
    // カレントである状態で呼び出すこと（GUI初期化後に呼ぶ）。
    bool init(int width, int height);

    // RGB24（1ピクセルあたり3バイト、パディング無し）のバッファを
    // テクスチャへ転送する。サイズが init() 時と異なる場合は
    // 内部で再確保する。
    bool upload(const uint8_t* rgb_data, int width, int height);

    // 確保済みのOpenGLテクスチャID。init() 前は 0。
    uint32_t texture_id() const { return texture_id_; }

    int width() const { return width_; }
    int height() const { return height_; }

    // テクスチャを解放する。デストラクタからも呼ばれる。
    void release();

    bool is_initialized() const { return texture_id_ != 0; }

private:
    bool allocate_texture(int width, int height);

    uint32_t texture_id_ = 0;
    int width_ = 0;
    int height_ = 0;
};

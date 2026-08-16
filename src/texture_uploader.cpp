#include "texture_uploader.h"

#include <iostream>

#if defined(__APPLE__)
    #include <OpenGL/gl.h>
#elif defined(_WIN32)
    #include <windows.h>
    #include <GL/gl.h>
#else
    #include <GL/gl.h>
#endif

// 補足: glGenTextures / glTexImage2D / glTexSubImage2D はいずれも OpenGL 1.1
// のコア機能なので、現時点ではGLADやGLEWのようなローダーは不要。
// ただし将来シェーダー(glCreateShader等、GL 2.0+)を使い始めたら
// ローダーの導入が必要になる（特にWindowsのopengl32.dllは1.1相当のAPIしか
// 直接エクスポートしていないため）。GUIフェーズで留意すること。

TextureUploader::TextureUploader() = default;

TextureUploader::~TextureUploader() {
    release();
}

bool TextureUploader::init(int width, int height) {
    if (width <= 0 || height <= 0) {
        std::cerr << "[TextureUploader] Invalid dimensions: " << width << "x" << height << std::endl;
        return false;
    }
    return allocate_texture(width, height);
}

bool TextureUploader::allocate_texture(int width, int height) {
    release();

    glGenTextures(1, &texture_id_);
    if (texture_id_ == 0) {
        std::cerr << "[TextureUploader] glGenTextures failed (is there a current GL context?)" << std::endl;
        return false;
    }

    glBindTexture(GL_TEXTURE_2D, texture_id_);

    // 動画フレームなのでミップマップは不要。毎フレーム張り替える前提で
    // 補間だけ設定しておく。
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    // FFmpeg側のRGB24はデフォルトで4バイトアライメントされていないため、
    // ここを合わせないと解像度によっては映像が斜めにズレる。
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

    glTexImage2D(
        GL_TEXTURE_2D, 0, GL_RGB,
        width, height, 0,
        GL_RGB, GL_UNSIGNED_BYTE, nullptr);

    glBindTexture(GL_TEXTURE_2D, 0);

    GLenum err = glGetError();
    if (err != GL_NO_ERROR) {
        std::cerr << "[TextureUploader] GL error during texture allocation: " << err << std::endl;
        release();
        return false;
    }

    width_ = width;
    height_ = height;
    return true;
}

bool TextureUploader::upload(const uint8_t* rgb_data, int width, int height) {
    if (!rgb_data) {
        std::cerr << "[TextureUploader] upload() called with null data" << std::endl;
        return false;
    }

    // サイズが変わっていたら(解像度が可変な動画、等)テクスチャを取り直す
    if (texture_id_ == 0 || width != width_ || height != height_) {
        if (!allocate_texture(width, height)) {
            return false;
        }
    }

    glBindTexture(GL_TEXTURE_2D, texture_id_);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

    // glTexImage2Dで作り直すのではなく glTexSubImage2D で同一メモリ領域を
    // 更新することで、毎フレームのGPU側再確保コストを避ける。
    glTexSubImage2D(
        GL_TEXTURE_2D, 0,
        0, 0, width, height,
        GL_RGB, GL_UNSIGNED_BYTE, rgb_data);

    glBindTexture(GL_TEXTURE_2D, 0);

    GLenum err = glGetError();
    if (err != GL_NO_ERROR) {
        std::cerr << "[TextureUploader] GL error during upload: " << err << std::endl;
        return false;
    }

    return true;
}

void TextureUploader::release() {
    if (texture_id_ != 0) {
        glDeleteTextures(1, &texture_id_);
        texture_id_ = 0;
    }
    width_ = 0;
    height_ = 0;
}

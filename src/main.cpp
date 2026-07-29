#include <iostream>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>

int main() {
    std::cout << "aural-engine starting...\n";
    std::cout << "FFmpeg version: " << av_version_info() << std::endl;

    // 後でここにデコード処理を書く
    return 0;
}

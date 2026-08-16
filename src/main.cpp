extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
}

#include <iostream>

int main() {
    std::cout << "aural-engine starting...\n";
    std::cout << "FFmpeg version: " << av_version_info() << std::endl;
    return 0;
}

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
}
#include <iostream>

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <video_file>\n";
        return 1;
    }

    const char* filename = argv[1];
    AVFormatContext* fmt_ctx = nullptr;

    // ファイルを開く
    if (avformat_open_input(&fmt_ctx, filename, nullptr, nullptr) < 0) {
        std::cerr << "Could not open file: " << filename << std::endl;
        return 1;
    }

    // ストリーム情報を読み込む
    if (avformat_find_stream_info(fmt_ctx, nullptr) < 0) {
        std::cerr << "Could not find stream info\n";
        avformat_close_input(&fmt_ctx);
        return 1;
    }

    // ファイル情報をダンプ（デバッグ用）
    av_dump_format(fmt_ctx, 0, filename, 0);

    std::cout << "File opened successfully!\n";
    std::cout << "Number of streams: " << fmt_ctx->nb_streams << std::endl;

    // 各ストリームの情報を表示
    for (unsigned int i = 0; i < fmt_ctx->nb_streams; i++) {
        AVStream* stream = fmt_ctx->streams[i];
        AVCodecParameters* codecpar = stream->codecpar;
        std::cout << "Stream " << i << ": ";
        if (codecpar->codec_type == AVMEDIA_TYPE_VIDEO) {
            std::cout << "VIDEO, codec: " << avcodec_get_name(codecpar->codec_id)
                      << ", " << codecpar->width << "x" << codecpar->height;
        } else if (codecpar->codec_type == AVMEDIA_TYPE_AUDIO) {
            std::cout << "AUDIO, codec: " << avcodec_get_name(codecpar->codec_id)
                      << ", channels: " << codecpar->ch_layout.nb_channels
                      << ", sample_rate: " << codecpar->sample_rate;
        } else {
            std::cout << "OTHER";
        }
        std::cout << std::endl;
    }

    avformat_close_input(&fmt_ctx);
    return 0;
}

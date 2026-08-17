#include "video_decoder.h"

#include <cstring>

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace {

// DecodedFrame.rgb_data を numpy配列(height, width, 3) の形でPython側へ渡す。
// メモリはコピーする(pybind11::array_tの寿命管理をシンプルに保つため)。
// 将来的にコピーコストがボトルネックになった場合は、capsule経由で
// VideoDecoder内部バッファを直接参照するzero-copy化を検討する。
py::array_t<uint8_t> frame_to_numpy(const DecodedFrame& frame) {
    py::array_t<uint8_t> arr({ frame.height, frame.width, 3 });
    std::memcpy(arr.mutable_data(), frame.rgb_data.data(), frame.rgb_data.size());
    return arr;
}

} // namespace

// Pythonから見たインターフェース:
//   import aural_engine
//   dec = aural_engine.VideoDecoder()
//   dec.open("input.mp4")
//   frame = dec.decode_next_frame()   # None または (numpy配列, pts_seconds)
//   dec.width, dec.height, dec.fps, dec.duration_seconds
//   dec.seek(1.5)
//   dec.close()
PYBIND11_MODULE(aural_engine, m) {
    m.doc() = "aural Studio C++ engine bindings (VideoDecoder etc.)";

    py::class_<VideoDecoder>(m, "VideoDecoder")
        .def(py::init<>())
        .def("open", &VideoDecoder::open, py::arg("filename"),
             "動画ファイルを開く。成功時True。")
        .def("decode_next_frame",
            [](VideoDecoder& self) -> py::object {
                DecodedFrame frame;
                if (!self.decode_next_frame(frame)) {
                    return py::none();
                }
                // (numpy配列 [H,W,3], pts_seconds) のタプルを返す
                return py::make_tuple(frame_to_numpy(frame), frame.pts_seconds);
            },
            "次の1フレームをデコードする。終端/エラー時はNoneを返す。\n"
            "成功時は (numpy.ndarray[H,W,3] uint8, pts_seconds: float) のタプル。")
        .def("seek", &VideoDecoder::seek, py::arg("seconds"),
             "指定秒数へシークする(キーフレーム単位)。")
        .def("close", &VideoDecoder::close, "開いたリソースを解放する。")
        .def("is_open", &VideoDecoder::is_open)
        .def_property_readonly("width", &VideoDecoder::width)
        .def_property_readonly("height", &VideoDecoder::height)
        .def_property_readonly("fps", &VideoDecoder::fps)
        .def_property_readonly("duration_seconds", &VideoDecoder::duration_seconds);
}

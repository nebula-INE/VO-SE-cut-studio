cmake_minimum_required(VERSION 3.15)
project(aural-engine VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(PkgConfig REQUIRED)
pkg_check_modules(FFMPEG REQUIRED
    libavcodec
    libavformat
    libavutil
    libavfilter
    libswscale
)

# デバッグ出力（ビルドログで確認）
message(STATUS "FFMPEG_INCLUDE_DIRS = ${FFMPEG_INCLUDE_DIRS}")
message(STATUS "FFMPEG_LIBRARY_DIRS = ${FFMPEG_LIBRARY_DIRS}")
message(STATUS "FFMPEG_LIBRARIES   = ${FFMPEG_LIBRARIES}")

# --- aural-engine: デコーダ単体の動作確認用ドライバ ---
add_executable(aural-engine
    src/main.cpp
    src/video_decoder.cpp
    src/texture_uploader.cpp
)

target_include_directories(aural-engine PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${FFMPEG_INCLUDE_DIRS}
)
target_link_directories(aural-engine PRIVATE ${FFMPEG_LIBRARY_DIRS})
target_link_libraries(aural-engine ${FFMPEG_LIBRARIES})

if(APPLE)
    target_link_libraries(aural-engine "-framework OpenGL")
elseif(UNIX)
    find_package(OpenGL REQUIRED)
    target_link_libraries(aural-engine ${OPENGL_LIBRARIES})
elseif(WIN32)
    target_link_libraries(aural-engine opengl32)
endif()

# --- aural-compose: 背景+オーバーレイ合成 -> エンコード書き出しパイプライン ---
add_executable(aural-compose
    src/compose_main.cpp
    src/video_decoder.cpp
    src/video_encoder.cpp
    src/compositor.cpp
)

target_include_directories(aural-compose PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${FFMPEG_INCLUDE_DIRS}
)
target_link_directories(aural-compose PRIVATE ${FFMPEG_LIBRARY_DIRS})
target_link_libraries(aural-compose ${FFMPEG_LIBRARIES})

# --- aural_engine: PySide6 GUIから使うPython拡張モジュール(pybind11) ---
# GUIはPySide6(Python側、python/preview.py)に統一したため、GLFWベースの
# aural-guiはビルド対象から外している。gui_main.cpp/texture_uploader.*は
# 将来QOpenGLWidget等で高性能プレビューが必要になった際の参考実装として
# リポジトリには残しているが、現状は未使用。
execute_process(
    COMMAND python3 -m pybind11 --cmakedir
    OUTPUT_VARIABLE pybind11_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
)
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(aural_engine
    src/py_bindings.cpp
    src/video_decoder.cpp
)

target_include_directories(aural_engine PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${FFMPEG_INCLUDE_DIRS}
)
target_link_directories(aural_engine PRIVATE ${FFMPEG_LIBRARY_DIRS})
target_link_libraries(aural_engine PRIVATE ${FFMPEG_LIBRARIES})

# --- vose_core: VO-SE音声合成エンジン(WORLDボコーダーベース) ---
# Python側からはctypes(python/vose_engine.py)経由で呼び出す。
# BigVGAN(ニューラルボコーダー後段)はONNX Runtimeへの依存が増えるため、
# ここではFreeティール相当(VOSE_PRO未定義、WORLD直接出力のみ)としてビルドする。
# Proティール(BigVGAN有効)が必要になったら、VOSE_PROを定義し、
# find_package等でonnxruntimeを解決した上でリンクを追加すること。
file(GLOB VOSE_WORLD_SOURCES ${CMAKE_SOURCE_DIR}/src/world/*.cpp)

add_library(vose_core SHARED
    src/vose_core.cpp
    ${VOSE_WORLD_SOURCES}
)

target_include_directories(vose_core PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/include/world
)

find_package(Threads REQUIRED)
target_link_libraries(vose_core PRIVATE Threads::Threads)

# 拡張子・prefixをプラットフォーム標準に合わせつつ、Python(ctypes)側の
# デフォルト読み込みパス(python/vose_engine.pyの_default_lib_name)と
# 名前が一致するようにする。
set_target_properties(vose_core PROPERTIES
    PREFIX "lib"
    OUTPUT_NAME "vose_core"
)

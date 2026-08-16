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

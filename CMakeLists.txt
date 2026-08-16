cmake_minimum_required(VERSION 3.15)
project(aural-engine VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(PkgConfig REQUIRED)
pkg_check_modules(FFMPEG REQUIRED libavcodec libavformat libavutil libswscale)

# デバッグ出力（ビルドログで確認）
message(STATUS "FFMPEG_INCLUDE_DIRS = ${FFMPEG_INCLUDE_DIRS}")
message(STATUS "FFMPEG_LIBRARY_DIRS = ${FFMPEG_LIBRARY_DIRS}")
message(STATUS "FFMPEG_LIBRARIES   = ${FFMPEG_LIBRARIES}")

add_executable(aural-engine
    src/main.cpp
    src/video_decoder.cpp
    src/texture_uploader.cpp
)

# 自作ヘッダー(video_decoder.h, texture_uploader.h)の置き場所
target_include_directories(aural-engine PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${FFMPEG_INCLUDE_DIRS}
)
target_link_directories(aural-engine PRIVATE ${FFMPEG_LIBRARY_DIRS})
target_link_libraries(aural-engine ${FFMPEG_LIBRARIES})

# OpenGL
if(APPLE)
    target_link_libraries(aural-engine "-framework OpenGL")
elseif(UNIX)
    find_package(OpenGL REQUIRED)
    target_link_libraries(aural-engine ${OPENGL_LIBRARIES})
elseif(WIN32)
    target_link_libraries(aural-engine opengl32)
endif()

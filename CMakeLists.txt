cmake_minimum_required(VERSION 3.15)
project(aural-engine VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(PkgConfig REQUIRED)

# Windowsではvcpkg経由で見つかることを想定
pkg_check_modules(FFMPEG REQUIRED
    libavcodec
    libavformat
    libavutil
    libswscale
)

add_executable(aural-engine
    src/main.cpp
    src/video_decoder.cpp
    src/texture_uploader.cpp
)

target_include_directories(aural-engine PRIVATE ${FFMPEG_INCLUDE_DIRS})
target_link_libraries(aural-engine ${FFMPEG_LIBRARIES})

# OpenGLは後で追加（Linux: GL, macOS: OpenGL.framework, Windows: opengl32）
if(APPLE)
    target_link_libraries(aural-engine "-framework OpenGL")
elseif(UNIX)
    find_package(OpenGL REQUIRED)
    target_link_libraries(aural-engine ${OPENGL_LIBRARIES})
elseif(WIN32)
    target_link_libraries(aural-engine opengl32)
endif()

cmake_minimum_required(VERSION 3.20)

# Honour CMAKE_OSX_ARCHITECTURES from the environment so that cibuildwheel can
# cross-compile x86_64 wheels from an arm64 runner without extra toolchain files.
if(APPLE AND DEFINED ENV{CMAKE_OSX_ARCHITECTURES} AND NOT CMAKE_OSX_ARCHITECTURES)
    set(CMAKE_OSX_ARCHITECTURES "$ENV{CMAKE_OSX_ARCHITECTURES}"
        CACHE STRING "Target macOS architectures" FORCE)
endif()

project(nng_python LANGUAGES C CXX)

# ── Profiling helpers ─────────────────────────────────────────────────────────
# Build with: cmake -DPROFILE=ON ...
# Adds -g + -fno-omit-frame-pointer to both the extension and nng so that
# `perf record --call-graph fp` can unwind native stacks.
option(PROFILE "Enable debug symbols and frame pointers for perf profiling" OFF)
if(PROFILE)
    message(STATUS "PROFILE=ON: adding -g -fno-omit-frame-pointer")
    add_compile_options(-g -fno-omit-frame-pointer)
    add_link_options(-g)
endif()

# ── nng submodule (built as a static library) ─────────────────────────────────
set(NNG_TESTS         OFF CACHE BOOL "" FORCE)
set(NNG_TOOLS         OFF CACHE BOOL "" FORCE)
set(NNG_ENABLE_NNGCAT OFF CACHE BOOL "" FORCE)
set(NNG_ENABLE_STATS  OFF CACHE BOOL "" FORCE)
set(NNG_ELIDE_DEPRECATED  ON CACHE BOOL "" FORCE)
set(NNG_ENABLE_HTTP  OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
# Required so the static lib can be linked into a shared Python extension
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
add_subdirectory(thirdparty/nng)

# ── Python + Cython ────────────────────────────────────────────────────────────
find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_program(CYTHON_EXECUTABLE cython REQUIRED
    HINTS ENV VIRTUAL_ENV PATH_SUFFIXES bin Scripts)

set(NNG_PYX   ${CMAKE_CURRENT_SOURCE_DIR}/nng/_nng.pyx)
set(NNG_C_OUT ${CMAKE_CURRENT_BINARY_DIR}/_nng.cpp)

# Collect all Cython inputs so CMake knows when to re-run Cython
file(GLOB NNG_CYTHON_DEPS
    ${CMAKE_CURRENT_SOURCE_DIR}/nng/*.pyx
    ${CMAKE_CURRENT_SOURCE_DIR}/nng/*.pxd
    ${CMAKE_CURRENT_SOURCE_DIR}/nng/*.pxi
    ${CMAKE_CURRENT_SOURCE_DIR}/nng/*.h
    ${CMAKE_CURRENT_SOURCE_DIR}/nng/cpp/*.hpp)

add_custom_command(
    OUTPUT  ${NNG_C_OUT}
    COMMAND ${CYTHON_EXECUTABLE}
            -3
            --cplus
            --output-file ${NNG_C_OUT}
            -I ${CMAKE_CURRENT_SOURCE_DIR}
            -I ${CMAKE_CURRENT_SOURCE_DIR}/nng
            ${NNG_PYX}
    DEPENDS ${NNG_CYTHON_DEPS}
    COMMENT "Cythonizing nng/_nng.pyx")

Python_add_library(_nng MODULE WITH_SOABI ${NNG_C_OUT})

set_target_properties(_nng PROPERTIES CXX_STANDARD 20 CXX_STANDARD_REQUIRED ON)

target_link_libraries(_nng PRIVATE nng)

# nng only exports its includes post-install; provide the build-tree path too
# The project root is included so that "nng/cpp/socket.hpp" etc. resolve correctly.
target_include_directories(_nng PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/nng
    ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/nng/include)

target_compile_options(_nng PRIVATE)

install(TARGETS _nng DESTINATION nng)

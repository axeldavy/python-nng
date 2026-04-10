cmake_minimum_required(VERSION 3.20)

# Honour CMAKE_OSX_ARCHITECTURES from the environment so that cibuildwheel can
# cross-compile x86_64 wheels from an arm64 runner without extra toolchain files.
if(APPLE AND DEFINED ENV{CMAKE_OSX_ARCHITECTURES} AND NOT CMAKE_OSX_ARCHITECTURES)
    set(CMAKE_OSX_ARCHITECTURES "$ENV{CMAKE_OSX_ARCHITECTURES}"
        CACHE STRING "Target macOS architectures" FORCE)
endif()

project(nng_python LANGUAGES C CXX)

# Required so every static lib (nng, Mbed TLS, …) can be linked into the shared
# Python extension.  Must be set before any add_subdirectory / FetchContent call.
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

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

# ── Optional Mbed TLS via FetchContent ───────────────────────────────────────
# When NNG_FETCH_MBEDTLS=ON, Mbed TLS 4.1.0 is downloaded at configure time,
# compiled as a static library, and linked into nng automatically.
# nng's cmake detects the resulting `mbedtls` target and links it directly,
# bypassing its own find_package call (see
# thirdparty/nng/src/supplemental/tls/mbedtls/CMakeLists.txt).
# Requires NNG_ENABLE_TLS=ON and NNG_TLS_ENGINE=mbed.
option(NNG_FETCH_MBEDTLS "Download and build Mbed TLS 4.1.0 automatically" OFF)
if(NNG_FETCH_MBEDTLS)
    message(STATUS "NNG_FETCH_MBEDTLS=ON: downloading Mbed TLS 4.1.0 via FetchContent")
    include(FetchContent)

    # Disable everything not needed for a static library embedded in a wheel.
    set(ENABLE_TESTING             OFF CACHE BOOL "" FORCE)
    set(ENABLE_PROGRAMS            OFF CACHE BOOL "" FORCE)
    set(MBEDTLS_FATAL_WARNINGS     OFF CACHE BOOL "" FORCE)
    set(USE_STATIC_MBEDTLS_LIBRARY ON  CACHE BOOL "" FORCE)
    set(USE_SHARED_MBEDTLS_LIBRARY OFF CACHE BOOL "" FORCE)

    FetchContent_Declare(mbedtls
        URL      https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-4.1.0/mbedtls-4.1.0.tar.bz2
        URL_HASH SHA256=377a09cf8eb81b5fb2707045e5522d5489d3309fed5006c9874e60558fc81d10
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    )
    FetchContent_MakeAvailable(mbedtls)
endif()

# ── nng submodule (built as a static library) ─────────────────────────────────
set(NNG_TESTS         OFF CACHE BOOL "" FORCE)
set(NNG_TOOLS         OFF CACHE BOOL "" FORCE)
set(NNG_ENABLE_NNGCAT OFF CACHE BOOL "" FORCE)
set(NNG_ENABLE_STATS  OFF CACHE BOOL "" FORCE)
set(NNG_ELIDE_DEPRECATED  ON CACHE BOOL "" FORCE)
set(NNG_ENABLE_HTTP  OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
add_subdirectory(thirdparty/nng)

# ── Satisfy cmake's export validation for FetchContent Mbed TLS ──────────────
# cmake requires every non-IMPORTED link dependency of an exported target to
# appear in an export set — even PRIVATE ones for static libraries.  nng's
# src/CMakeLists.txt calls both install(EXPORT nng-target) and
# export(EXPORT nng-target) unconditionally, so the Mbed TLS targets produced
# by FetchContent must join that same export set.  EXCLUDE_FROM_ALL keeps them
# out of a normal `cmake --install` run; they exist solely to satisfy the
# cmake generate-step validation.
if(NNG_FETCH_MBEDTLS)
    foreach(_mbed_tgt IN ITEMS mbedtls mbedx509 tfpsacrypto mbedcrypto)
        if(TARGET ${_mbed_tgt})
            install(TARGETS ${_mbed_tgt}
                EXPORT nng-target
                ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}"
                EXCLUDE_FROM_ALL
            )
        endif()
    endforeach()
endif()

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

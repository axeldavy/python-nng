cmake_minimum_required(VERSION 3.20)
project(nng_python LANGUAGES C CXX)

# ── nng submodule (built as a static library) ─────────────────────────────────
set(NNG_TESTS         OFF CACHE BOOL "" FORCE)
set(NNG_TOOLS         OFF CACHE BOOL "" FORCE)
set(NNG_ENABLE_NNGCAT OFF CACHE BOOL "" FORCE)
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
    ${CMAKE_CURRENT_SOURCE_DIR}/nng/*.h)

add_custom_command(
    OUTPUT  ${NNG_C_OUT}
    COMMAND ${CYTHON_EXECUTABLE}
            -3
            --cplus
            --output-file ${NNG_C_OUT}
            -I ${CMAKE_CURRENT_SOURCE_DIR}/nng
            ${NNG_PYX}
    DEPENDS ${NNG_CYTHON_DEPS}
    COMMENT "Cythonizing nng/_nng.pyx")

Python_add_library(_nng MODULE WITH_SOABI ${NNG_C_OUT})

target_link_libraries(_nng PRIVATE nng)

# nng only exports its includes post-install; provide the build-tree path too
target_include_directories(_nng PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/nng
    ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/nng/include)

# GCC 14 promoted -Wincompatible-pointer-types to an error.
# Cython-generated C code relies on implicit PyObject*/extension-type-ptr
# conversions that are valid but trigger this warning.
target_compile_options(_nng PRIVATE -Wno-incompatible-pointer-types)

install(TARGETS _nng DESTINATION nng)

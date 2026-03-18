cmake_minimum_required(VERSION 3.20)
project(bench_c_nng LANGUAGES C)

# ── Locate the nng submodule from the workspace root ─────────────────────────
# This CMakeLists is meant to be built standalone inside
# benchmarks/competitors/c_nng/build/.
# The nng submodule lives at <workspace_root>/thirdparty/nng.

# Allow the caller to override the workspace root.
if(NOT DEFINED WORKSPACE_ROOT)
    # Default: four levels up from this file's directory
    get_filename_component(WORKSPACE_ROOT
        "${CMAKE_CURRENT_SOURCE_DIR}/../../.."
        ABSOLUTE)
endif()

set(NNG_SUBMODULE "${WORKSPACE_ROOT}/thirdparty/nng")
if(NOT EXISTS "${NNG_SUBMODULE}/CMakeLists.txt")
    message(FATAL_ERROR
        "nng submodule not found at ${NNG_SUBMODULE}. "
        "Pass -DWORKSPACE_ROOT=<path> to point to the correct location.")
endif()

# Configure nng as a static library embedded in this build
set(NNG_TESTS         OFF CACHE BOOL "" FORCE)
set(NNG_TOOLS         OFF CACHE BOOL "" FORCE)
set(NNG_ENABLE_NNGCAT OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)

add_subdirectory("${NNG_SUBMODULE}" nng_build)

# On POSIX, math functions live in libm; on Windows they are part of the CRT.
if(NOT WIN32)
    set(_MATH_LIB m)
endif()

# ── bench_server ─────────────────────────────────────────────────────────────
add_executable(bench_server bench_server.c)
target_link_libraries(bench_server PRIVATE nng)
target_include_directories(bench_server PRIVATE "${NNG_SUBMODULE}/include")

# ── bench_client_lat ─────────────────────────────────────────────────────────
add_executable(bench_client_lat bench_client_lat.c)
target_link_libraries(bench_client_lat PRIVATE nng ${_MATH_LIB})
target_include_directories(bench_client_lat PRIVATE "${NNG_SUBMODULE}/include")

# ── bench_client_ops ─────────────────────────────────────────────────────────
add_executable(bench_client_ops bench_client_ops.c)
target_link_libraries(bench_client_ops PRIVATE nng ${_MATH_LIB})
target_include_directories(bench_client_ops PRIVATE "${NNG_SUBMODULE}/include")

# ── bench_inproc_lat ─────────────────────────────────────────────────────────
# Server thread + REQ client in one process — required for inproc:// latency.
add_executable(bench_inproc_lat bench_inproc_lat.c)
target_link_libraries(bench_inproc_lat PRIVATE nng ${_MATH_LIB})
target_include_directories(bench_inproc_lat PRIVATE "${NNG_SUBMODULE}/include")

# ── bench_inproc_ops ─────────────────────────────────────────────────────────
# Server thread + REQ client in one process — required for inproc:// ops/sec.
add_executable(bench_inproc_ops bench_inproc_ops.c)
target_link_libraries(bench_inproc_ops PRIVATE nng ${_MATH_LIB})
target_include_directories(bench_inproc_ops PRIVATE "${NNG_SUBMODULE}/include")

# Compiler settings
foreach(_tgt bench_server bench_client_lat bench_client_ops bench_inproc_lat bench_inproc_ops)
    target_compile_options(${_tgt} PRIVATE
        $<$<C_COMPILER_ID:GNU,Clang>:-O2 -Wall -Wextra>)
endforeach()

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

# ── bench_server ─────────────────────────────────────────────────────────────
add_executable(bench_server bench_server.c)
target_link_libraries(bench_server PRIVATE nng)
target_include_directories(bench_server PRIVATE "${NNG_SUBMODULE}/include")

# ── bench_client ─────────────────────────────────────────────────────────────
add_executable(bench_client bench_client.c)
target_link_libraries(bench_client PRIVATE nng m)
target_include_directories(bench_client PRIVATE "${NNG_SUBMODULE}/include")

# Compiler settings
foreach(_tgt bench_server bench_client)
    target_compile_options(${_tgt} PRIVATE
        $<$<C_COMPILER_ID:GNU,Clang>:-O2 -Wall -Wextra>)
endforeach()

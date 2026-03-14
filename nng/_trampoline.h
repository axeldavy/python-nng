/* nng/_trampoline.h
 *
 * Minimal C++ header included by _nng.pyx to ensure nng.h and Python.h
 * are both visible before Cython's generated code references them.
 * The actual trampoline functions are written in Cython (_aio.pxi, _socket.pxi).
 */
#pragma once
#include "nng/nng.h"

#ifdef __cplusplus
#include <thread>
#include <functional>
#include <cstddef>

/* Returns a size_t hash of the calling thread's std::thread::id.
 * Used by _AioOp to detect whether __dealloc__ is running on the nng
 * callback thread (→ nng_aio_reap) or another thread (→ nng_aio_free). */
inline std::size_t nng_py_current_thread_id() noexcept {
    return std::hash<std::thread::id>{}(std::this_thread::get_id());
}
#endif

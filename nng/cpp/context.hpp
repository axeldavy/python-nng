// nng/cpp/context.hpp
// RAII wrapper for nng_ctx. No Python headers included.
// C++20. Moveable, non-copyable.
//
// Holds a shared_ptr<SocketHandle> so the socket is guaranteed to outlive
// its contexts regardless of Python GC ordering.
#pragma once
#include <memory>
#include <nng/nng.h>
#include "socket.hpp"

namespace nng_cpp {

class ContextHandle {
public:
    ContextHandle() noexcept : _ctx{0} {}

    // Takes ownership of an already-opened nng_ctx.
    // sock must be non-null; the shared_ptr keeps the socket alive.
    ContextHandle(nng_ctx ctx, std::shared_ptr<SocketHandle> sock) noexcept
        : _ctx(ctx), _socket(std::move(sock)) {}

    ContextHandle(const ContextHandle&)            = delete;
    ContextHandle& operator=(const ContextHandle&) = delete;

    ContextHandle(ContextHandle&& o) noexcept
        : _ctx(o._ctx), _socket(std::move(o._socket)) { o._ctx.id = 0; }
    ContextHandle& operator=(ContextHandle&& o) noexcept {
        if (this != &o) {
            close_if_open();
            _ctx    = o._ctx;
            _socket = std::move(o._socket);
            o._ctx.id = 0;
        }
        return *this;
    }

    ~ContextHandle() { close_if_open(); }

    // Explicit close; returns nng error code.
    [[nodiscard]] int close() noexcept {
        if (_ctx.id == 0) return 0;
        int rv = nng_ctx_close(_ctx);
        _ctx.id = 0;
        return rv;
    }

    nng_ctx raw()     const noexcept { return _ctx; }
    bool    is_open() const noexcept { return _ctx.id != 0; }

private:
    void close_if_open() noexcept {
        if (_ctx.id != 0) { nng_ctx_close(_ctx); _ctx.id = 0; }
    }

    nng_ctx                         _ctx;
    std::shared_ptr<SocketHandle>   _socket; // keeps socket alive while context lives
};

} // namespace nng_cpp

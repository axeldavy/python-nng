// nng/cpp/aio.hpp
// RAII wrapper for nng_aio*. No Python headers included.
// C++20. Moveable, non-copyable.
//
// Holds an anchor (either a shared_ptr<ContextHandle> or a shared_ptr<SocketHandle>)
// so the anchored object (and its parent socket) cannot be closed before a pending
// async operation completes. Pass the anchor at construction time.
#pragma once
#include <memory>
#include <nng/nng.h>
#include <thread>
#include "context.hpp"

namespace nng_cpp {

class AioHandle {
public:
    AioHandle() noexcept : _aio(nullptr) {}
    explicit AioHandle(nng_aio* a) noexcept : _aio(a) {}
    AioHandle(nng_aio* a, std::shared_ptr<ContextHandle> anchor) noexcept
        : _aio(a), _ctx_anchor(std::move(anchor)) {}
    AioHandle(nng_aio* a, std::shared_ptr<SocketHandle> anchor) noexcept
        : _aio(a), _sock_anchor(std::move(anchor)) {}

    AioHandle(const AioHandle&)            = delete;
    AioHandle& operator=(const AioHandle&) = delete;

    AioHandle(AioHandle&& o) noexcept
        : _aio(o._aio)
        , _ctx_anchor(std::move(o._ctx_anchor))
        , _sock_anchor(std::move(o._sock_anchor))
    {
        o._aio = nullptr;
    }

    AioHandle& operator=(AioHandle&& o) noexcept {
        if (this != &o) {
            destroy();
            _aio             = o._aio;
            _ctx_anchor      = std::move(o._ctx_anchor);
            _sock_anchor     = std::move(o._sock_anchor);
            o._aio           = nullptr;
        }
        return *this;
    }

    ~AioHandle() { destroy(); }

    nng_aio* get()      const noexcept { return _aio; }
    bool     is_valid() const noexcept { return _aio != nullptr; }

private:
    void destroy() noexcept {
        if (!_aio) return;
        // Note: by design we never free in the callback thread.
        if (nng_aio_busy(_aio)) {
            nng_aio_reap(_aio);
        } else {
            nng_aio_free(_aio);
        }
        _aio = nullptr;
        _ctx_anchor.reset();  // release anchors only after the nng_aio is freed
        _sock_anchor.reset();
    }

    nng_aio*                        _aio             = nullptr;
    std::shared_ptr<ContextHandle>  _ctx_anchor;      // keeps context alive during ctx op
    std::shared_ptr<SocketHandle>   _sock_anchor;     // keeps socket alive during socket op
};

} // namespace nng_cpp

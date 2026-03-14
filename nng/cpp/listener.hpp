// nng/cpp/listener.hpp
// RAII wrapper for nng_listener. No Python headers included.
// C++20. Moveable, non-copyable.
//
// Holds a shared_ptr<SocketHandle> so the socket is guaranteed to outlive
// its listeners regardless of Python GC ordering.
//
// All nng_listener operations are exposed as inline methods so Cython code
// never needs to call raw() to reach the raw nng_listener.
#pragma once
#include <memory>
#include <nng/nng.h>
#include "socket.hpp"

namespace nng_cpp {

class ListenerHandle {
public:
    ListenerHandle() noexcept : _l{0} {}

    // Takes ownership of an already-created nng_listener.
    // sock must be non-null; the shared_ptr keeps the socket alive.
    ListenerHandle(nng_listener l, std::shared_ptr<SocketHandle> sock) noexcept
        : _l(l), _socket(std::move(sock)) {}

    ListenerHandle(const ListenerHandle&)            = delete;
    ListenerHandle& operator=(const ListenerHandle&) = delete;

    ListenerHandle(ListenerHandle&& o) noexcept
        : _l(o._l), _socket(std::move(o._socket)) { o._l.id = 0; }
    ListenerHandle& operator=(ListenerHandle&& o) noexcept {
        if (this != &o) {
            close_if_open();
            _l      = o._l;
            _socket = std::move(o._socket);
            o._l.id = 0;
        }
        return *this;
    }

    ~ListenerHandle() { close_if_open(); }

    // ── Ownership ─────────────────────────────────────────────────────────

    explicit operator bool() const noexcept { return _l.id != 0; }
    bool is_open()           const noexcept { return _l.id != 0; }

    // Explicit close; returns nng error code.
    [[nodiscard]] int close() noexcept {
        if (_l.id == 0) return 0;
        int rv = nng_listener_close(_l);
        _l.id  = 0;
        return rv;
    }

    // ── nng_listener operations ───────────────────────────────────────────

    int id() const noexcept { return nng_listener_id(_l); }

    int get_ms(const char* opt, nng_duration* v) const noexcept {
        return nng_listener_get_ms(_l, opt, v);
    }
    int set_ms(const char* opt, nng_duration v) noexcept {
        return nng_listener_set_ms(_l, opt, v);
    }
    int set_tls(nng_tls_config* cfg) noexcept {
        return nng_listener_set_tls(_l, cfg);
    }
    int start() noexcept { return nng_listener_start(_l, 0); }

    // Raw accessor kept for interop with nng APIs not yet wrapped.
    nng_listener raw() const noexcept { return _l; }

private:
    void close_if_open() noexcept {
        if (_l.id != 0) { nng_listener_close(_l); _l.id = 0; }
    }

    nng_listener                    _l;
    std::shared_ptr<SocketHandle>   _socket; // keeps socket alive while listener lives
};

} // namespace nng_cpp

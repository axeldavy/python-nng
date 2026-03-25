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

    ~ListenerHandle() = default;
    // NOT { close_if_open(); }
    // The reason is that ListenerHandle destruction in our codebase
    // can only occur if the Listener is released. And as Listener
    // is referenced by the socket, it can only happen if Socket is released.
    // Since we want to allow releasing Socket while having contexts alive,
    // we must delay the actual listener close until the shared_ptr<SocketHandle>
    // refcount drops to zero. The SocketHandle destructor will call close() on
    // the socket which closes all its listeners, including this one.

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

    int get_port(int *port) const noexcept {
        return nng_listener_get_int(_l, NNG_OPT_BOUND_PORT, port);
    }
    int get_recv_max_size(size_t* v) const noexcept {
        return nng_listener_get_size(_l, NNG_OPT_RECVMAXSZ, v);
    }

    int set_recv_max_size(size_t v) noexcept {
        return nng_listener_set_size(_l, NNG_OPT_RECVMAXSZ, v);
    }
    int set_tls(nng_tls_config* cfg) noexcept {
        return nng_listener_set_tls(_l, cfg);
    }
    int start() noexcept { return nng_listener_start(_l, 0); }

    // ── TCP options ───────────────────────────────────────────────────────

    // TCP_NODELAY: disable Nagle's algorithm when true (default true).
    int get_nodelay(bool* v) const noexcept {
        return nng_listener_get_bool(_l, NNG_OPT_TCP_NODELAY, v);
    }
    int set_nodelay(bool v) noexcept {
        return nng_listener_set_bool(_l, NNG_OPT_TCP_NODELAY, v);
    }

    // TCP_KEEPALIVE: enable TCP keep-alive probes when true (default false).
    int get_keepalive(bool* v) const noexcept {
        return nng_listener_get_bool(_l, NNG_OPT_TCP_KEEPALIVE, v);
    }
    int set_keepalive(bool v) noexcept {
        return nng_listener_set_bool(_l, NNG_OPT_TCP_KEEPALIVE, v);
    }

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

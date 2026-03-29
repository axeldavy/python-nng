// nng/cpp/dialer.hpp
// RAII wrapper for nng_dialer. No Python headers included.
// C++20. Moveable, non-copyable.
//
// Holds a shared_ptr<SocketHandle> so the socket is guaranteed to outlive
// its dialers regardless of Python GC ordering.
//
// All nng_dialer operations are exposed as inline methods so Cython code
// never needs to call raw() to reach the raw nng_dialer.
#pragma once
#include <memory>
#include <nng/nng.h>
#include "socket.hpp"
#include "tls_config.hpp"

namespace nng_cpp {

class DialerHandle {
public:
    DialerHandle() noexcept : _d{0} {}

    // Takes ownership of an already-created nng_dialer.
    // sock must be non-null; the shared_ptr keeps the socket alive.
    DialerHandle(nng_dialer d, std::shared_ptr<SocketHandle> sock) noexcept
        : _d(d), _socket(std::move(sock)) {}

    DialerHandle(const DialerHandle&)            = delete;
    DialerHandle& operator=(const DialerHandle&) = delete;

    DialerHandle(DialerHandle&& o) noexcept
        : _d(o._d), _socket(std::move(o._socket)) { o._d.id = 0; }
    DialerHandle& operator=(DialerHandle&& o) noexcept {
        if (this != &o) {
            close_if_open();
            _d      = o._d;
            _socket = std::move(o._socket);
            o._d.id = 0;
        }
        return *this;
    }

    ~DialerHandle() = default;
    // NOT { close_if_open(); }
    // The reason is that DialerHandle destruction in our codebase
    // can only occur if the Dialer is released. And as Dialer
    // is referenced by the socket, it can only happen if Socket is released.
    // Since we want to allow releasing Socket while having contexts alive,
    // we must delay the actual dialer close until the shared_ptr<SocketHandle>
    // refcount drops to zero. The SocketHandle destructor will call close() on
    // the socket which closes all its dialers, including this one.

    // ── Ownership ─────────────────────────────────────────────────────────

    explicit operator bool() const noexcept { return _d.id != 0; }
    bool is_open()           const noexcept { return _d.id != 0; }

    // Explicit close; returns nng error code.
    [[nodiscard]] int close() noexcept {
        if (_d.id == 0) return 0;
        int rv = nng_dialer_close(_d);
        _d.id  = 0;
        return rv;
    }

    // ── nng_dialer operations ─────────────────────────────────────────────

    int id() const noexcept { return nng_dialer_id(_d); }


    int get_reconnect_time_min_ms(nng_duration* v) const noexcept {
        return nng_dialer_get_ms(_d, NNG_OPT_RECONNMINT, v);
    }
    int get_reconnect_time_max_ms(nng_duration* v) const noexcept {
        return nng_dialer_get_ms(_d, NNG_OPT_RECONNMAXT, v);
    }
    int get_recv_max_size(size_t* v) const noexcept {
        return nng_dialer_get_size(_d, NNG_OPT_RECVMAXSZ, v);
    }

    int set_reconnect_time_min_ms(nng_duration v) noexcept {
        return nng_dialer_set_ms(_d, NNG_OPT_RECONNMINT, v);
    }
    int set_reconnect_time_max_ms(nng_duration v) noexcept {
        return nng_dialer_set_ms(_d, NNG_OPT_RECONNMAXT, v);
    }
    int set_recv_max_size(size_t v) noexcept {
        return nng_dialer_set_size(_d, NNG_OPT_RECVMAXSZ, v);
    }
    // Takes shared ownership of the TLS config so the dialer keeps it alive
    // for its own lifetime, regardless of Python GC on the TlsConfig object.
    int set_tls(std::shared_ptr<TlsConfigHandle> cfg) noexcept {
        _tls_cfg = std::move(cfg);
        return nng_dialer_set_tls(_d, _tls_cfg->get());
    }
    int start(int flags) noexcept { return nng_dialer_start(_d, flags); }

    // ── TCP options ───────────────────────────────────────────────────────

    // TCP_NODELAY: disable Nagle's algorithm when true (default true).
    int get_nodelay(bool* v) const noexcept {
        return nng_dialer_get_bool(_d, NNG_OPT_TCP_NODELAY, v);
    }
    int set_nodelay(bool v) noexcept {
        return nng_dialer_set_bool(_d, NNG_OPT_TCP_NODELAY, v);
    }

    // TCP_KEEPALIVE: enable TCP keep-alive probes when true (default false).
    int get_keepalive(bool* v) const noexcept {
        return nng_dialer_get_bool(_d, NNG_OPT_TCP_KEEPALIVE, v);
    }
    int set_keepalive(bool v) noexcept {
        return nng_dialer_set_bool(_d, NNG_OPT_TCP_KEEPALIVE, v);
    }

    // LOCADDR: source address for outgoing connections.
    // get_local_addr is only meaningful after the connection has been
    // established; returns the actual bound local address.
    // set_local_addr must be called before start() to bind to a specific
    // local IP (useful on multi-homed hosts). The port in *sa is ignored
    // by nng; the OS assigns an ephemeral port.
    int get_local_addr(nng_sockaddr* sa) const noexcept {
        return nng_dialer_get_addr(_d, NNG_OPT_LOCADDR, sa);
    }
    int set_local_addr(const nng_sockaddr* sa) noexcept {
        return nng_dialer_set_addr(_d, NNG_OPT_LOCADDR, sa);
    }

    // Raw accessor kept for interop with nng APIs not yet wrapped.
    nng_dialer raw() const noexcept { return _d; }

private:
    void close_if_open() noexcept {
        if (_d.id != 0) { nng_dialer_close(_d); _d.id = 0; }
    }

    nng_dialer                      _d;
    std::shared_ptr<SocketHandle>   _socket;     // keeps socket alive while dialer lives
    std::shared_ptr<TlsConfigHandle> _tls_cfg;   // keeps TLS config alive while dialer lives
};

} // namespace nng_cpp

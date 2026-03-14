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

    ~DialerHandle() { close_if_open(); }

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

    int get_ms(const char* opt, nng_duration* v) const noexcept {
        return nng_dialer_get_ms(_d, opt, v);
    }
    int set_ms(const char* opt, nng_duration v) noexcept {
        return nng_dialer_set_ms(_d, opt, v);
    }
    int set_tls(nng_tls_config* cfg) noexcept {
        return nng_dialer_set_tls(_d, cfg);
    }
    int start(int flags) noexcept { return nng_dialer_start(_d, flags); }

    // Raw accessor kept for interop with nng APIs not yet wrapped.
    nng_dialer raw() const noexcept { return _d; }

private:
    void close_if_open() noexcept {
        if (_d.id != 0) { nng_dialer_close(_d); _d.id = 0; }
    }

    nng_dialer                      _d;
    std::shared_ptr<SocketHandle>   _socket; // keeps socket alive while dialer lives
};

} // namespace nng_cpp

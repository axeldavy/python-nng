// nng/cpp/socket.hpp
// RAII wrapper for nng_socket. No Python headers included.
// C++20. Moveable, non-copyable.
#pragma once
#include <nng/nng.h>

namespace nng_cpp {

class SocketHandle {
public:
    SocketHandle() noexcept : _sock{0} {}
    explicit SocketHandle(nng_socket s) noexcept : _sock(s) {}

    SocketHandle(const SocketHandle&)            = delete;
    SocketHandle& operator=(const SocketHandle&) = delete;

    SocketHandle(SocketHandle&& o) noexcept : _sock(o._sock) { o._sock.id = 0; }
    SocketHandle& operator=(SocketHandle&& o) noexcept {
        if (this != &o) { close_if_open(); _sock = o._sock; o._sock.id = 0; }
        return *this;
    }

    ~SocketHandle() { close_if_open(); }

    // Explicit close; returns the nng error code (0 = OK). Sets internal id to 0.
    [[nodiscard]] int close() noexcept {
        if (_sock.id == 0) return 0;
        int rv = nng_socket_close(_sock);
        _sock.id = 0;
        return rv;
    }

    nng_socket raw()     const noexcept { return _sock; }
    bool       is_open() const noexcept { return _sock.id != 0; }

private:
    void close_if_open() noexcept {
        if (_sock.id != 0) { nng_socket_close(_sock); _sock.id = 0; }
    }

    nng_socket _sock;
};

} // namespace nng_cpp

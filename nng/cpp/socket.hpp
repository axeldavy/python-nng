// nng/cpp/socket.hpp
// RAII wrapper for nng_socket. No Python headers included.
// C++20. Non moveable, non-copyable.
#pragma once
#include <nng/nng.h>
#include "pipe.hpp"

namespace nng_cpp {

class SocketHandle {
public:
    SocketHandle() noexcept : _sock{0} {}
    explicit SocketHandle(nng_socket s) noexcept : _sock(s) {}

    SocketHandle(const SocketHandle&)            = delete;
    SocketHandle& operator=(const SocketHandle&) = delete;

    SocketHandle(SocketHandle&& o) = delete;
    SocketHandle& operator=(SocketHandle&& o) = delete;

    ~SocketHandle() { close_if_open(); }

    // Explicit close; returns the nng error code (0 = OK). Sets internal id to 0.
    [[nodiscard]] int close() noexcept {
        if (_sock.id == 0) return 0;
        // Clear notify callbacks to avoid calls on a closed socket
        nng_pipe_notify(_sock, NNG_PIPE_EV_ADD_PRE, nullptr, nullptr);
        nng_pipe_notify(_sock, NNG_PIPE_EV_ADD_POST, nullptr, nullptr);
        nng_pipe_notify(_sock, NNG_PIPE_EV_REM_POST, nullptr, nullptr);

        // close the socket and set id to 0 to mark as closed.
        int rv = nng_socket_close(_sock);
        _sock.id = 0;
        return rv;
    }

    nng_socket raw()     const noexcept { return _sock; }
    bool       is_open() const noexcept { return _sock.id != 0; }

    // ── PipeCollection delegation ──────────────────────────────────────────

    PipeCollection* pipe_collection() noexcept { return &_pipes; }

private:
    void close_if_open() noexcept {
        if (_sock.id != 0) {
            // Clear notify callbacks to avoid calls on a closed socket
            nng_pipe_notify(_sock, NNG_PIPE_EV_ADD_PRE, nullptr, nullptr);
            nng_pipe_notify(_sock, NNG_PIPE_EV_ADD_POST, nullptr, nullptr);
            nng_pipe_notify(_sock, NNG_PIPE_EV_REM_POST, nullptr, nullptr);

            // Close the socket and set id to 0 to mark as closed.
            nng_socket_close(_sock); _sock.id = 0;
        }
    }

    nng_socket     _sock;
    PipeCollection _pipes;
};

} // namespace nng_cpp
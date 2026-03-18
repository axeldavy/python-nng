// nng/cpp/pipe.hpp
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <nng/nng.h>

namespace nng_cpp {

// ─────────────────────────────────────────────────────────────────────────────
// PipeHandle — non-owning wrapper for nng_pipe.
//
// nng_pipe is a struct (passed by value), so there is nothing to free.
// PipeHandle never closes the pipe in its destructor.  Call close() explicitly
// when you want to tear the connection down from Python code.
// ─────────────────────────────────────────────────────────────────────────────
class PipeHandle {
public:
    // ── Constructors ──────────────────────────────────────────────────────

    PipeHandle() noexcept : _pipe(NNG_PIPE_INITIALIZER) {}
    explicit PipeHandle(nng_pipe p) noexcept : _pipe(p) {}

    // Copyable and moveable — the struct is trivial.
    PipeHandle(const PipeHandle&)            = default;
    PipeHandle& operator=(const PipeHandle&) = default;
    PipeHandle(PipeHandle&&)                 = default;
    PipeHandle& operator=(PipeHandle&&)      = default;

    ~PipeHandle() = default;    // non-owning: no close

    // ── Raw access ────────────────────────────────────────────────────────

    nng_pipe get() const noexcept { return _pipe; }

    // ── Identity ──────────────────────────────────────────────────────────

    // Returns a positive id for a valid pipe, or -1.
    int  get_id()   const noexcept { return nng_pipe_id(_pipe); }
    bool is_valid() const noexcept { return nng_pipe_id(_pipe) > 0; }
    explicit operator bool() const noexcept { return is_valid(); }

    // ── Lifecycle ─────────────────────────────────────────────────────────

    // Closes the underlying connection.  Returns 0 on success, nng error code otherwise.
    int close() noexcept { return nng_pipe_close(_pipe); }

    // ── Creator accessors ─────────────────────────────────────────────────

    nng_dialer   get_dialer()   const noexcept { return nng_pipe_dialer(_pipe); }
    nng_listener get_listener() const noexcept { return nng_pipe_listener(_pipe); }
    nng_socket   get_socket()   const noexcept { return nng_pipe_socket(_pipe); }

    // ── Addresses ─────────────────────────────────────────────────────────

    // Fills *sa with the remote peer's address.  Returns 0 on success.
    int peer_addr(nng_sockaddr& sa) const noexcept { return nng_pipe_peer_addr(_pipe, &sa); }

    // Fills *sa with the local (self) address.  Returns 0 on success.
    int self_addr(nng_sockaddr& sa) const noexcept { return nng_pipe_self_addr(_pipe, &sa); }

    // ── Equality (id-based) ───────────────────────────────────────────────

    bool operator==(const PipeHandle& o) const noexcept {
        return nng_pipe_id(_pipe) == nng_pipe_id(o._pipe);
    }
    bool operator!=(const PipeHandle& o) const noexcept { return !(*this == o); }

private:
    nng_pipe _pipe;
};

} // namespace nng_cpp

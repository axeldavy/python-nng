// nng/cpp/factory.hpp
// Free-function factories that span multiple handle types.
// Kept separate to avoid circular includes: socket.hpp cannot include
// context.hpp/dialer.hpp/listener.hpp (they already include socket.hpp).
//
// Each function takes an int& err out-parameter that receives the nng error
// code on failure (0 on success). On failure an empty (closed) handle is
// returned and the nng resource is properly cleaned up (RAII via the
// DialerHandle / ListenerHandle destructors).
//
// No Python headers included. C++20.
#pragma once
#include <memory>
#include <nng/nng.h>
#include "context.hpp"
#include "dialer.hpp"
#include "listener.hpp"

namespace nng_cpp {

// ── Context ───────────────────────────────────────────────────────────────────

// Open a new nng_ctx on *sock* and return a shared_ptr<ContextHandle>.
// shared_ptr (not unique_ptr) because AioHandle may anchor a copy of it.
inline std::shared_ptr<ContextHandle>
make_context(const std::shared_ptr<SocketHandle>& sock, int& err) noexcept {
    nng_ctx ctx{0};
    err = nng_ctx_open(&ctx, sock->raw());
    if (err != 0) return nullptr;
    return std::make_shared<ContextHandle>(ctx, sock);
}

// ── Dialer ────────────────────────────────────────────────────────────────────

// Create a new nng_dialer on *sock* for *url* and return a unique_ptr<DialerHandle>.
// If nng_dialer_create succeeds, the DialerHandle is constructed (RAII);
// any subsequent failure (option setting, etc.) must call .close() explicitly
// before discarding the unique_ptr — or just let the unique_ptr destructor
// call Close automatically.
inline DialerHandle
make_dialer(const std::shared_ptr<SocketHandle>& sock,
            const char* url, int& err) noexcept {
    nng_dialer d{0};
    err = nng_dialer_create(&d, sock->raw(), url);
    if (err != 0) return DialerHandle{};
    return DialerHandle{d, sock};
}

// ── Listener ──────────────────────────────────────────────────────────────────

// Create a new nng_listener on *sock* for *url* and return a unique_ptr<ListenerHandle>.
inline ListenerHandle
make_listener(const std::shared_ptr<SocketHandle>& sock,
              const char* url, int& err) noexcept {
    nng_listener l{0};
    err = nng_listener_create(&l, sock->raw(), url);
    if (err != 0) return ListenerHandle{};
    return ListenerHandle{l, sock};
}

} // namespace nng_cpp

// nng/cpp/tls_config.hpp
// RAII wrapper for nng_tls_config*. No Python headers included.
// C++20. Moveable, non-copyable.
#pragma once
#include <memory>
#include <nng/nng.h>

namespace nng_cpp {

class TlsConfigHandle {
public:
    TlsConfigHandle() noexcept : _cfg(nullptr) {}
    explicit TlsConfigHandle(nng_tls_config* cfg) noexcept : _cfg(cfg) {}

    TlsConfigHandle(const TlsConfigHandle&)            = delete;
    TlsConfigHandle& operator=(const TlsConfigHandle&) = delete;

    TlsConfigHandle(TlsConfigHandle&& o) noexcept : _cfg(o._cfg) { o._cfg = nullptr; }
    TlsConfigHandle& operator=(TlsConfigHandle&& o) noexcept {
        if (this != &o) { free_if_owned(); _cfg = o._cfg; o._cfg = nullptr; }
        return *this;
    }

    ~TlsConfigHandle() { free_if_owned(); }

    nng_tls_config* get()      const noexcept { return _cfg; }
    bool            is_valid() const noexcept { return _cfg != nullptr; }

    // ── Static factory ───────────────────────────────────────────────────

    // Allocate a TLS config for the given mode (client or server).
    // On success returns a non-null unique_ptr and sets err=0.
    // On failure returns nullptr and sets err to the nng error code.
    static std::unique_ptr<TlsConfigHandle> alloc(nng_tls_mode mode, int& err) noexcept {
        nng_tls_config* cfg = nullptr;
        err = nng_tls_config_alloc(&cfg, mode);
        if (err != 0) return nullptr;
        return std::make_unique<TlsConfigHandle>(cfg);
    }

private:
    void free_if_owned() noexcept {
        if (_cfg) { nng_tls_config_free(_cfg); _cfg = nullptr; }
    }

    nng_tls_config* _cfg;
};

} // namespace nng_cpp

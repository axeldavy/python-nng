// nng/cpp/pipe.hpp
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <nng/nng.h>

namespace nng_cpp {

// ─────────────────────────────────────────────────────────────────────────────
// PipeHandle — Pipe metadata holder (one instance per pipe).
//
// All fields (dialer, listener, socket, peer/self addresses) are captured
// from nng while the pipe is still alive (ADD_PRE time).
//
// Not Thread-safe (although only set_status() modifies state).
// ─────────────────────────────────────────────────────────────────────────────
class PipeHandle {
public:
    // ── Constructors ──────────────────────────────────────────────────────

    PipeHandle() noexcept
        : _pipe(NNG_PIPE_INITIALIZER), _id(-1),
          _dialer{0}, _listener{0}, _socket{0},
          _peer_addr{}, _peer_addr_err(0),
          _self_addr{}, _self_addr_err(0),
          _nodelay(true), _keepalive(false),
          _tls_verified(false),
          _status(0) {}

    // Capture all metadata while the nng_pipe handle is still valid.
    explicit PipeHandle(nng_pipe p) noexcept
        : _pipe(p),
          _id(nng_pipe_id(p)),
          _dialer(nng_pipe_dialer(p)),
          _listener(nng_pipe_listener(p)),
          _socket(nng_pipe_socket(p)),
          _peer_addr{},
          _peer_addr_err(nng_pipe_peer_addr(p, &_peer_addr)),
          _self_addr{},
          _self_addr_err(nng_pipe_self_addr(p, &_self_addr)),
          _nodelay(true), _keepalive(false),
          _tls_verified(false),
          _status(0),
          _peer_pid(-1) {
        // Cache TCP boolean options; ignore errors (non-TCP transports return
        // NNG_ENOTSUP and the defaults of true/false are correct in that case).
        nng_pipe_get_bool(p, NNG_OPT_TCP_NODELAY,   &_nodelay);
        nng_pipe_get_bool(p, NNG_OPT_TCP_KEEPALIVE, &_keepalive);

        // ── TLS snapshot (captured once; valid after the pipe handle dies) ─

        // Was the peer certificate verified?
        nng_pipe_get_bool(p, NNG_OPT_TLS_VERIFIED, &_tls_verified);

        // Common name from the peer certificate (empty string if non-TLS).
        // nng_pipe_get_string returns a pointer to nng-managed storage (not
        // an allocated copy), so it must NOT be freed by the caller.
        {
            const char* cn = nullptr;
            if (nng_pipe_get_string(p, NNG_OPT_TLS_PEER_CN, &cn) == 0 && cn) {
                _tls_peer_cn = cn;
            }
        }

        // DER encoding of the peer certificate (empty if non-TLS / no cert).
        {
            nng_tls_cert* cert = nullptr;
            if (nng_pipe_peer_cert(p, &cert) == 0 && cert != nullptr) {
                size_t der_size = 0;
                nng_tls_cert_der(cert, nullptr, &der_size);
                if (der_size > 0) {
                    _peer_cert_der.resize(der_size);
                    nng_tls_cert_der(cert, _peer_cert_der.data(), &der_size);
                    _peer_cert_der.resize(der_size);
                }
                nng_tls_cert_free(cert);
            }
        }
    
        // Optional: capture peer PID if supported by the transport (e.g. AF_UNIX).
        if (nng_pipe_get_int(p, NNG_OPT_PEER_PID, &_peer_pid) != 0) {
            _peer_pid = -1; // mark as unsupported
        }
    }

    // Non-copyable: each PipeHandle instance corresponds to exactly one pipe.
    PipeHandle(const PipeHandle&)            = delete;
    PipeHandle& operator=(const PipeHandle&) = delete;
    PipeHandle(PipeHandle&&)                 = default;
    PipeHandle& operator=(PipeHandle&&)      = default;

    ~PipeHandle() = default;    // non-owning: no close

    // ── Identity ──────────────────────────────────────────────────────────

    // Returns the cached pipe id (always valid, even after pipe destruction).
    int  get_id()   const noexcept { return _id; }

    // Calls nng to test whether the underlying pipe is still live.
    bool is_valid() const noexcept { return nng_pipe_id(_pipe) > 0; }
    explicit operator bool() const noexcept { return is_valid(); }

    // ── Lifecycle ─────────────────────────────────────────────────────────

    // Closes the underlying connection.  Returns 0 on success.
    int close() noexcept { return nng_pipe_close(_pipe); }

    // ── Creator accessors (cached, no nng calls) ──────────────────────────

    nng_dialer   get_dialer()   const noexcept { return _dialer; }
    nng_listener get_listener() const noexcept { return _listener; }
    nng_socket   get_socket()   const noexcept { return _socket; }

    // ── Addresses (cached, no nng calls) ──────────────────────────────────

    // Copies the cached peer address into *sa.
    // Returns the error code from the original nng_pipe_peer_addr() call;
    // 0 means the address was captured successfully.
    int peer_addr(nng_sockaddr& sa) const noexcept {
        sa = _peer_addr;
        return _peer_addr_err;
    }

    // Copies the cached local (self) address into *sa.
    // Returns the error code from the original nng_pipe_self_addr() call.
    int self_addr(nng_sockaddr& sa) const noexcept {
        sa = _self_addr;
        return _self_addr_err;
    }

    int get_peer_pid() const noexcept { return _peer_pid; }

    // ── TCP options (cached, no nng calls) ────────────────────────────────

    // Whether TCP_NODELAY (Nagle disabled) was active at connection time.
    bool get_nodelay() const noexcept { return _nodelay; }

    // Whether TCP_KEEPALIVE was active at connection time.
    bool get_keepalive() const noexcept { return _keepalive; }

    // ── TLS properties (cached at connection time, no nng calls) ──────────

    // Whether the peer TLS certificate was verified at connection time.
    // Always false for non-TLS pipes.
    bool get_tls_verified() const noexcept { return _tls_verified; }

    // Common name (CN) from the peer certificate, or nullptr for non-TLS
    // pipes / connections without a peer certificate.
    // The returned pointer is valid for the lifetime of this PipeHandle.
    const char* get_tls_peer_cn() const noexcept {
        return _tls_peer_cn.empty() ? nullptr : _tls_peer_cn.c_str();
    }

    // Returns true if a peer certificate DER blob was captured, filling
    // *buf and *sz.  *buf points into this PipeHandle's internal storage;
    // the pointer is valid for the lifetime of this PipeHandle.
    // Returns false for non-TLS pipes or connections without a peer cert.
    bool get_peer_cert_der(const uint8_t** buf, size_t* sz) const noexcept {
        if (_peer_cert_der.empty()) return false;
        *buf = _peer_cert_der.data();
        *sz  = _peer_cert_der.size();
        return true;
    }

    // ── Status (managed by PipeCollection) ──────────────────────────────

    int  get_status() const noexcept { return _status; }
    void set_status(int s)  noexcept { _status = s; }

    // ── Equality (id-based) ───────────────────────────────────────────────

    bool operator==(const PipeHandle& o) const noexcept { return _id == o._id; }
    bool operator!=(const PipeHandle& o) const noexcept { return !(*this == o); }

private:
    nng_pipe     _pipe;
    int          _id;
    nng_dialer   _dialer;
    nng_listener _listener;
    nng_socket   _socket;
    nng_sockaddr _peer_addr;
    int          _peer_addr_err;
    int          _peer_pid;    // optional: NNG_OPT_PEER_PID. -1 if unsupported
    nng_sockaddr _self_addr;
    int          _self_addr_err;
    bool         _nodelay;                 // TCP_NODELAY at connection time
    bool         _keepalive;              // TCP_KEEPALIVE at connection time
    bool         _tls_verified;           // TLS peer cert verified at connection time
    std::string  _tls_peer_cn;            // TLS peer CN (empty if non-TLS / no cert)
    std::vector<uint8_t> _peer_cert_der;  // DER of peer cert (empty if non-TLS / no cert)
    int          _status;
};


// ─────────────────────────────────────────────────────────────────────────────
// PipeCollection — insertion-ordered collection of shared PipeHandle instances
// for a single socket.
//
// Callers retain shared_ptr<PipeHandle> references so that pipe metadata
// remains accessible after a pipe is removed from the collection.
//
// Thread-safe
// ─────────────────────────────────────────────────────────────────────────────
class PipeCollection {
public:
    PipeCollection()  = default;
    ~PipeCollection() {
        for (const auto& pipe : _pipes)
            pipe->set_status(NNG_PIPE_EV_REM_POST);
    }

    PipeCollection(const PipeCollection&)            = delete;
    PipeCollection& operator=(const PipeCollection&) = delete;
    PipeCollection(PipeCollection&&)                 = delete;
    PipeCollection& operator=(PipeCollection&&)      = delete;

    // ── Event handling ─────────────────────────────────────────────────

    void handle_event(nng_pipe pipe, nng_pipe_ev ev) {
        std::lock_guard<std::mutex> lock(_mutex);
        if (ev == NNG_PIPE_EV_ADD_PRE) {
            auto pipe_h = add(pipe);
            pipe_h->set_status(NNG_PIPE_EV_ADD_PRE);
        }
        else if (ev == NNG_PIPE_EV_ADD_POST) {
            auto pipe_h = find(nng_pipe_id(pipe));
            if (pipe_h) {
                pipe_h->set_status(NNG_PIPE_EV_ADD_POST);
            } else {
                // This should never happen: we should have already added the pipe on ADD_PRE.
                // But if it does, we can still recover by adding it now.
                pipe_h = add(pipe);
                pipe_h->set_status(NNG_PIPE_EV_ADD_POST);
            }
        }
        else if (ev == NNG_PIPE_EV_REM_POST) {
            remove(pipe); // sets NNG_PIPE_EV_REM_POST
        }
        _had_events = true;
    }

    std::vector<std::shared_ptr<PipeHandle>> get_pipes() const noexcept {
        std::lock_guard<std::mutex> lock(_mutex);
        return _pipes;
    }

    // Remove all entries.
    void clear() noexcept {
        std::lock_guard<std::mutex> lock(_mutex);
        for (const auto& pipe : _pipes)
            pipe->set_status(NNG_PIPE_EV_REM_POST);
        _pipes.clear();
        _had_events = true;
    }

    // Returns true if any events have been handled since the last call to had_events().
    bool had_events() noexcept {
        std::lock_guard<std::mutex> lock(_mutex);
        if (!_had_events) return false;
        _had_events = false;
        return true;
    }

private:
    // All functions below assume the lock held.

    // ── Lookup ────────────────────────────────────────────────────────────

    // Return the shared_ptr for *pipe_id*, or nullptr if not found.
    std::shared_ptr<PipeHandle> find(int pipe_id) const noexcept {
        for (const auto& h : _pipes)
            if (h->get_id() == pipe_id)
                return h;
        return nullptr;
    }

    // ── Mutation ──────────────────────────────────────────────────────────

    std::shared_ptr<PipeHandle> add(nng_pipe p) {
        auto h = std::make_shared<PipeHandle>(p);
        _pipes.push_back(h);
        return h;
    }

    void remove(nng_pipe pipe) noexcept {
        int pipe_id = nng_pipe_id(pipe);
        for (auto it = _pipes.begin(); it != _pipes.end(); ++it) {
            if ((*it)->get_id() == pipe_id) {
                (*it)->set_status(NNG_PIPE_EV_REM_POST);
                _pipes.erase(it);
                return;
            }
        }
    }

// ── Data ─────────────────────────────────────────────────────────────
    std::vector<std::shared_ptr<PipeHandle>> _pipes;
    mutable std::mutex                       _mutex;
    bool                                     _had_events = false;
};

} // namespace nng_cpp
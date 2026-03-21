//
// Copyright 2021 Staysail Systems, Inc. <info@staysail.tech>
//
// This software is supplied under the terms of the MIT License, a
// copy of which should be located in the distribution where this
// file was obtained (LICENSE.txt).  A copy of the license may also be
// found online at https://opensource.org/licenses/MIT.
//
// This file is adapted from nng's platform-specific pipe implementations

// nng/cpp/platform.hpp
// ─────────────────────────────────────────────────────────────────────────────
// Platform-specific fd-based dispatch queues for asyncio integration.
//
// These provide an alternative wakeup mechanism to the condition-variable-based
// DispatchQueue, allowing Python's asyncio event loop to watch a readable file
// descriptor for completion notifications.
//
// IDispatchQueue – abstract base; the interface used by _aio_sock_async.
// SockDispatchQueue  – socket-pair wakeup (AF_UNIX on POSIX, TCP on Windows).
//   Read end suitable for Python's socket.socket(fileno=) wrapping.
// PollDispatchQueue  – pipe-based wakeup on POSIX (raw fd), falling back to
//   SockDispatchQueue on Windows.  Suitable for loop.add_reader(fd, …).
//
// DispatchQueueContainer – lightweight per-AIO-op wrapper holding a shared_ptr
//   to the owning queue and the uint64_t op-id to push.  Allocated once per
//   _AioSockASync instance, passed as the void* arg to nng_aio_alloc, and kept
//   alive by the Python object.  The nng trampoline calls fire() to push op_id
//   to the queue and signal the read fd.
//
// No Python headers or nng headers are required.
#pragma once

#include <atomic>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>

#ifdef _WIN32
#    include <winsock2.h>
#    include <ws2tcpip.h>
#else
#    include <fcntl.h>
#    include <sys/socket.h>
#    include <sys/un.h>
#    include <unistd.h>
#endif

namespace nng_cpp {

// ─────────────────────────────────────────────────────────────────────────────
// IDispatchQueue – abstract interface shared by all notification-fd queues
// ─────────────────────────────────────────────────────────────────────────────

class IDispatchQueue {
public:
    virtual ~IDispatchQueue() = default;

    /// Enqueue *id* and write a wakeup byte to the write end.
    /// Safe to call from any thread without the GIL.
    virtual void push(uint64_t id) noexcept = 0;

    /// Non-blocking pop.  Returns true and sets *id* if an item is ready.
    virtual bool get_ready(uint64_t& id) noexcept = 0;

    /// Swap all pending items into *out* under a single lock acquisition.
    /// More efficient than repeated get_ready() when multiple items may be
    /// ready at once (e.g. burst of concurrent AIO completions).
    /// *out* need not be empty on entry; items are appended.
    virtual void drain_ready(std::deque<uint64_t>& out) noexcept = 0;

    /// File descriptor (or SOCKET cast to int on Windows) suitable for
    /// read-readiness monitoring via asyncio add_reader / select / poll.
    virtual int get_read_fd() const noexcept = 0;

    /// Drain all pending wakeup bytes without blocking.
    /// Must be called before (or within) the asyncio callback that drains
    /// items via get_ready(), to avoid spurious re-triggers on edge-triggered
    /// backends.
    virtual void drain_wakeup() noexcept = 0;

    IDispatchQueue(const IDispatchQueue&)            = delete;
    IDispatchQueue& operator=(const IDispatchQueue&) = delete;
    IDispatchQueue(IDispatchQueue&&)                 = delete;
    IDispatchQueue& operator=(IDispatchQueue&&)      = delete;

protected:
    IDispatchQueue() = default;
};

// ─────────────────────────────────────────────────────────────────────────────
// Platform implementations
// ─────────────────────────────────────────────────────────────────────────────

#ifdef _WIN32

// ── Windows: socket-pair wakeup using a loopback TCP connection ──────────────
//
// Windows named pipes cannot be used with select(), so we mirror the approach
// from nng's win_pipe.c: open a loopback TCP listener, connect a client, then
// accept to obtain a socket pair.  See that file for the detailed rationale.

class SockDispatchQueue final : public IDispatchQueue {
public:
    SockDispatchQueue() noexcept {
        SOCKET         afd = INVALID_SOCKET;
        struct sockaddr_in addr{};
        int            alen = sizeof(addr);
        int            one  = 1;
        ULONG          yes  = 1;

        // Bind to an ephemeral loopback port.
        addr.sin_family      = AF_INET;
        addr.sin_port        = 0;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

        afd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (afd == INVALID_SOCKET) return;

        setsockopt(afd, SOL_SOCKET, SO_EXCLUSIVEADDRUSE,
                   reinterpret_cast<char*>(&one), sizeof(one));
        if (bind(afd, reinterpret_cast<sockaddr*>(&addr), alen) != 0) goto fail;
        if (getsockname(afd, reinterpret_cast<sockaddr*>(&addr), &alen) != 0) goto fail;
        if (listen(afd, 1) != 0) goto fail;

        _read_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (_read_sock == INVALID_SOCKET) goto fail;
        if (connect(_read_sock, reinterpret_cast<sockaddr*>(&addr), alen) != 0) goto fail;

        _write_sock = accept(afd, nullptr, nullptr);
        if (_write_sock == INVALID_SOCKET) goto fail;

        // Mark both ends non-blocking.
        ioctlsocket(_read_sock,  FIONBIO, &yes);
        ioctlsocket(_write_sock, FIONBIO, &yes);
        closesocket(afd);
        return;

    fail:
        if (afd          != INVALID_SOCKET) closesocket(afd);
        if (_read_sock   != INVALID_SOCKET) { closesocket(_read_sock);  _read_sock  = INVALID_SOCKET; }
        if (_write_sock  != INVALID_SOCKET) { closesocket(_write_sock); _write_sock = INVALID_SOCKET; }
    }

    ~SockDispatchQueue() override {
        if (_read_sock  != INVALID_SOCKET) { closesocket(_read_sock);  _read_sock  = INVALID_SOCKET; }
        if (_write_sock != INVALID_SOCKET) { closesocket(_write_sock); _write_sock = INVALID_SOCKET; }
    }

    void push(uint64_t id) noexcept override {
        bool was_empty;
        {
            std::lock_guard<std::mutex> lk(_mutex);
            was_empty = _queue.empty();
            _queue.push_back(id);
        }
        // Only write a wakeup byte when the queue transitions from empty to
        // non-empty.  The reader drains all pending items in one shot, so a
        // single byte is sufficient to wake it for an entire burst.
        if (was_empty) {
            char c = 1;
            send(_write_sock, &c, 1, 0);
        }
    }

    bool get_ready(uint64_t& id) noexcept override {
        std::lock_guard<std::mutex> lk(_mutex);
        if (_queue.empty()) return false;
        id = _queue.front();
        _queue.pop_front();
        return true;
    }

    void drain_ready(std::deque<uint64_t>& out) noexcept override {
        std::lock_guard<std::mutex> lk(_mutex);
        if (!_queue.empty()) out.insert(out.end(), _queue.begin(), _queue.end());
        _queue.clear();
    }

    int get_read_fd() const noexcept override {
        return static_cast<int>(_read_sock);
    }

    void drain_wakeup() noexcept override {
        char buf[32];
        while (recv(_read_sock, buf, sizeof(buf), 0) > 0) {}
    }

private:
    SOCKET               _read_sock  = INVALID_SOCKET;
    SOCKET               _write_sock = INVALID_SOCKET;
    std::deque<uint64_t> _queue;
    std::mutex           _mutex;
};

// On Windows there is no POSIX pipe(); PollDispatchQueue falls back to the
// socket-pair implementation so client code compiles unchanged on both platforms.
using PollDispatchQueue = SockDispatchQueue;

#else // ── POSIX ────────────────────────────────────────────────────────────

// ── POSIX: socket-pair wakeup using AF_UNIX ──────────────────────────────────
//
// The read end is a real socket fd, so Python can wrap it with
// socket.socket(fileno=queue.get_read_fd()).

class SockDispatchQueue final : public IDispatchQueue {
public:
    SockDispatchQueue() noexcept {
        int fds[2] = {-1, -1};
        if (socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return;
        _read_sock  = fds[0];
        _write_sock = fds[1];
        fcntl(_read_sock,  F_SETFL, O_NONBLOCK);
        fcntl(_write_sock, F_SETFL, O_NONBLOCK);
        fcntl(_read_sock,  F_SETFD, FD_CLOEXEC);
        fcntl(_write_sock, F_SETFD, FD_CLOEXEC);
    }

    ~SockDispatchQueue() override {
        if (_read_sock  >= 0) { close(_read_sock);  _read_sock  = -1; }
        if (_write_sock >= 0) { close(_write_sock); _write_sock = -1; }
    }

    void push(uint64_t id) noexcept override {
        bool was_empty;
        {
            std::lock_guard<std::mutex> lk(_mutex);
            was_empty = _queue.empty();
            _queue.push_back(id);
        }
        if (was_empty) {
            char c = 1;
            (void)send(_write_sock, &c, 1, MSG_DONTWAIT);
        }
    }

    bool get_ready(uint64_t& id) noexcept override {
        std::lock_guard<std::mutex> lk(_mutex);
        if (_queue.empty()) return false;
        id = _queue.front();
        _queue.pop_front();
        return true;
    }

    void drain_ready(std::deque<uint64_t>& out) noexcept override {
        std::lock_guard<std::mutex> lk(_mutex);
        if (!_queue.empty()) out.insert(out.end(), _queue.begin(), _queue.end());
        _queue.clear();
    }

    int get_read_fd() const noexcept override { return _read_sock; }

    void drain_wakeup() noexcept override {
        char buf[32];
        while (recv(_read_sock, buf, sizeof(buf), MSG_DONTWAIT) > 0) {}
    }

private:
    int                  _read_sock  = -1;
    int                  _write_sock = -1;
    std::deque<uint64_t> _queue;
    std::mutex           _mutex;
};

// ── POSIX: pipe-based wakeup ──────────────────────────────────────────────────
//
// The read end is a raw POSIX fd, usable directly with loop.add_reader(fd, …).
// Unlike SockDispatchQueue it cannot be wrapped in socket.socket(fileno=).

class PollDispatchQueue final : public IDispatchQueue {
public:
    PollDispatchQueue() noexcept {
        int fds[2] = {-1, -1};
        if (pipe(fds) != 0) return;
        _read_fd  = fds[0];
        _write_fd = fds[1];
        fcntl(_read_fd,  F_SETFL, O_NONBLOCK);
        fcntl(_write_fd, F_SETFL, O_NONBLOCK);
        fcntl(_read_fd,  F_SETFD, FD_CLOEXEC);
        fcntl(_write_fd, F_SETFD, FD_CLOEXEC);
    }

    ~PollDispatchQueue() override {
        if (_read_fd  >= 0) { close(_read_fd);  _read_fd  = -1; }
        if (_write_fd >= 0) { close(_write_fd); _write_fd = -1; }
    }

    void push(uint64_t id) noexcept override {
        bool was_empty;
        {
            std::lock_guard<std::mutex> lk(_mutex);
            was_empty = _queue.empty();
            _queue.push_back(id);
        }
        if (was_empty) {
            char c = 1;
            (void)write(_write_fd, &c, 1);
        }
    }

    bool get_ready(uint64_t& id) noexcept override {
        std::lock_guard<std::mutex> lk(_mutex);
        if (_queue.empty()) return false;
        id = _queue.front();
        _queue.pop_front();
        return true;
    }

    void drain_ready(std::deque<uint64_t>& out) noexcept override {
        std::lock_guard<std::mutex> lk(_mutex);
        if (!_queue.empty()) out.insert(out.end(), _queue.begin(), _queue.end());
        _queue.clear();
    }

    int get_read_fd() const noexcept override { return _read_fd; }

    void drain_wakeup() noexcept override {
        char buf[32];
        while (read(_read_fd, buf, sizeof(buf)) > 0) {}
    }

private:
    int                  _read_fd  = -1;
    int                  _write_fd = -1;
    std::deque<uint64_t> _queue;
    std::mutex           _mutex;
};

#endif // _WIN32

// ─────────────────────────────────────────────────────────────────────────────
// DispatchQueueContainer – per-AIO-op lifetime anchor + queue reference
// ─────────────────────────────────────────────────────────────────────────────
//
// One instance is allocated per _AioSockASync object and stored as its owner
// (via unique_ptr).  The raw pointer is passed as the void* arg to
// nng_aio_alloc().  The nng callback trampoline casts arg back and calls
// fire(), which enqueues op_id in the queue and writes a wakeup byte to the
// notification fd.
//
// Keeping _queue as a shared_ptr means the queue stays alive for as long as
// any live _AioSockASync references it — even if the owning Socket is closed.

class DispatchQueueContainer {
public:
    DispatchQueueContainer(std::shared_ptr<IDispatchQueue> q, uint64_t op_id) noexcept
        : _queue(std::move(q)), _op_id(op_id) {}

    ~DispatchQueueContainer() = default;

    DispatchQueueContainer(const DispatchQueueContainer&)            = delete;
    DispatchQueueContainer& operator=(const DispatchQueueContainer&) = delete;
    DispatchQueueContainer(DispatchQueueContainer&&)                 = delete;
    DispatchQueueContainer& operator=(DispatchQueueContainer&&)      = delete;

    /// Called from the GIL-free nng trampoline: push op_id to the queue.
    void fire() noexcept {
        uint32_t expected = 0;
        if (_skip_point_reached.compare_exchange_strong(expected, 1, std::memory_order_acq_rel)) {
            // If we run before the skip point, we skip submission
            return;
        }
        _queue->push(_op_id);
    }

    bool reach_skip_point() noexcept {
        uint32_t expected = 0;
        if (_skip_point_reached.compare_exchange_strong(expected, 1, std::memory_order_acq_rel)) {
            // executing before fire() was called
            return false;
        }
        // executing after fire() was called
        return true;
    }

private:
    std::shared_ptr<IDispatchQueue> _queue;
    uint64_t                        _op_id;
    std::atomic<uint32_t>            _skip_point_reached{0}; // set when events are pushed, cleared by had_events()
};

// ─────────────────────────────────────────────────────────────────────────────
// Factory helpers – return shared_ptr<IDispatchQueue> to avoid manual upcasting
// ─────────────────────────────────────────────────────────────────────────────

inline std::shared_ptr<IDispatchQueue> make_sock_dispatch_queue() {
    return std::make_shared<SockDispatchQueue>();
}

inline std::shared_ptr<IDispatchQueue> make_poll_dispatch_queue() {
    return std::make_shared<PollDispatchQueue>();
}

} // namespace nng_cpp

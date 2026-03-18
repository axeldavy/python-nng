// nng/cpp/pipe_notif.hpp
// ─────────────────────────────────────────────────────────────────────────────
// GIL-free queue for nng pipe lifecycle events.
//
// The nng callback thread calls push() with no Python involvement.
// The AIO dispatcher thread drains it via get_ready() (non-blocking) after
// being woken by a sentinel push into the AIO DispatchQueue.
//
// Mirrors the shape of DispatchQueue but carries (pipe_id, event) pairs.
// No Python headers or nng headers required here.
#pragma once

#include <cstdint>
#include <deque>
#include <mutex>

namespace nng_cpp {

class PipeEventQueue {
public:
    PipeEventQueue() noexcept : _stopped(false) {}

    ~PipeEventQueue() noexcept { stop(); }

    // ── Producer side (nng callback thread, NO GIL) ───────────────────────

    // sock_id must be captured in the trampoline while the pipe is still alive;
    // by the time REM_POST is dispatched the pipe handle is already destroyed.
    void push(uint32_t sock_id, uint32_t pipe_id, int ev) noexcept {
        std::lock_guard<std::mutex> lk(_mutex);
        _queue.push_back({sock_id, pipe_id, ev});
    }

    // ── Consumer side (dispatcher thread, NO GIL) ─────────────────────────

    // Non-blocking pop.  Returns true and sets sock_id/pipe_id/ev when ready.
    bool get_ready(uint32_t& sock_id, uint32_t& pipe_id, int& ev) noexcept {
        std::lock_guard<std::mutex> lk(_mutex);
        if (_queue.empty())
            return false;
        sock_id = _queue.front().sock_id;
        pipe_id = _queue.front().pipe_id;
        ev      = _queue.front().ev;
        _queue.pop_front();
        return true;
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────

    void stop() noexcept {
        std::lock_guard<std::mutex> lk(_mutex);
        _stopped = true;
    }

    bool is_stopped() const noexcept { return _stopped; }

private:
    struct Entry { uint32_t sock_id; uint32_t pipe_id; int ev; };

    std::deque<Entry> _queue;
    std::mutex        _mutex;
    bool              _stopped;
};

} // namespace nng_cpp

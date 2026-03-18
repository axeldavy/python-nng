// nng/cpp/dispatch.hpp
// ─────────────────────────────────────────────────────────────────────────────
// GIL-free dispatch queue for nng async completion callbacks.
//
// The nng callback thread calls push(id) with no Python involvement.
// A dedicated Python dispatcher thread drains the queue via get_ready() /
// wait_for() (both called with the GIL released) and then resolves futures.
//
// No Python headers or nng headers are required here.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <mutex>

namespace nng_cpp {

class DispatchQueue {
public:
    DispatchQueue() noexcept : _stopped(false) {}

    ~DispatchQueue() noexcept {
        stop();
    }

    // ── Producer side (called from nng callback thread, NO GIL) ──────────

    // Enqueue an ID and wake the dispatcher.
    void push(uint64_t id) noexcept {
        {
            std::lock_guard<std::mutex> lk(_mutex);
            _queue.push_back(id);
        }
        _cv.notify_one();
    }

    // ── Consumer side (called from dispatcher thread, NO GIL) ────────────

    // Non-blocking poll.  Returns true and sets id if an item is ready.
    bool get_ready(uint64_t& id) noexcept {
        std::lock_guard<std::mutex> lk(_mutex);
        if (_queue.empty())
            return false;
        id = _queue.front();
        _queue.pop_front();
        return true;
    }

    // Blocking wait with a timeout.
    // Returns true and sets id if an item became available before timeout.
    // Returns false on timeout or when stopped-and-empty.
    bool wait_for(uint64_t& id, int timeout_ms) noexcept {
        std::unique_lock<std::mutex> lk(_mutex);
        bool woken = _cv.wait_for(
            lk,
            std::chrono::milliseconds(timeout_ms),
            [this] { return !_queue.empty() || _stopped; });

        if (woken && !_queue.empty()) {
            id = _queue.front();
            _queue.pop_front();
            return true;
        }
        return false; // timeout, or stopped with empty queue
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────

    // Signal the dispatcher to exit once the queue is drained.
    void stop() noexcept {
        {
            std::lock_guard<std::mutex> lk(_mutex);
            _stopped = true;
        }
        _cv.notify_all();
    }

    bool is_stopped() const noexcept { return _stopped; }

private:
    std::deque<uint64_t>    _queue;
    std::mutex              _mutex;
    std::condition_variable _cv;
    bool                    _stopped;
};

} // namespace nng_cpp

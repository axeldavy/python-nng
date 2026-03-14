// nng/cpp/message.hpp
// RAII wrapper for nng_msg*. No Python headers included.
// C++20. Moveable, non-copyable. Owns the message unless steal() is called.
//
// All nng_msg operations are exposed as inline methods so Cython code never
// needs to call .get() to reach the raw pointer.
#pragma once
#include <cstddef>
#include <cstdint>
#include <nng/nng.h>

namespace nng_cpp {

class MessageHandle {
public:
    // ── Constructors ──────────────────────────────────────────────────────

    MessageHandle() noexcept : _msg(nullptr) {}
    explicit MessageHandle(nng_msg* m) noexcept : _msg(m) {}

    MessageHandle(const MessageHandle&)            = delete;
    MessageHandle& operator=(const MessageHandle&) = delete;

    MessageHandle(MessageHandle&& o) noexcept : _msg(o._msg) { o._msg = nullptr; }
    MessageHandle& operator=(MessageHandle&& o) noexcept {
        if (this != &o) { free_if_owned(); _msg = o._msg; o._msg = nullptr; }
        return *this;
    }

    ~MessageHandle() { free_if_owned(); }

    // ── Ownership ─────────────────────────────────────────────────────────

    // Returns true when a live nng_msg* is held.
    explicit operator bool() const noexcept { return _msg != nullptr; }

    // Returns the raw pointer without transferring ownership.
    nng_msg* get()      const noexcept { return _msg; }
    bool     is_valid() const noexcept { return _msg != nullptr; }

    // Transfer ownership to the caller (for send). The handle becomes empty.
    nng_msg* steal() noexcept { nng_msg* m = _msg; _msg = nullptr; return m; }

    // Restore ownership (called when a send fails and nng returns the msg).
    void restore(nng_msg* m) noexcept { _msg = m; }

    // ── Body ──────────────────────────────────────────────────────────────

    std::size_t body_len() const noexcept { return nng_msg_len(_msg); }
    void*       body_ptr() const noexcept { return nng_msg_body(_msg); }
    void        clear()          noexcept { nng_msg_clear(_msg); }
    int  reserve(std::size_t n)  noexcept { return nng_msg_reserve(_msg, n); }
    int  append(const void* data, std::size_t len) noexcept { return nng_msg_append(_msg, data, len); }
    int  insert(const void* data, std::size_t len) noexcept { return nng_msg_insert(_msg, data, len); }
    int  trim(std::size_t n)     noexcept { return nng_msg_trim(_msg, n); }
    int  chop(std::size_t n)     noexcept { return nng_msg_chop(_msg, n); }

    // ── Typed body helpers (network byte-order) ───────────────────────────

    int append_u16(std::uint16_t v) noexcept { return nng_msg_append_u16(_msg, v); }
    int append_u32(std::uint32_t v) noexcept { return nng_msg_append_u32(_msg, v); }
    int append_u64(std::uint64_t v) noexcept { return nng_msg_append_u64(_msg, v); }

    int chop_u16(std::uint16_t* v) noexcept { return nng_msg_chop_u16(_msg, v); }
    int chop_u32(std::uint32_t* v) noexcept { return nng_msg_chop_u32(_msg, v); }
    int chop_u64(std::uint64_t* v) noexcept { return nng_msg_chop_u64(_msg, v); }

    int trim_u16(std::uint16_t* v) noexcept { return nng_msg_trim_u16(_msg, v); }
    int trim_u32(std::uint32_t* v) noexcept { return nng_msg_trim_u32(_msg, v); }
    int trim_u64(std::uint64_t* v) noexcept { return nng_msg_trim_u64(_msg, v); }

    // ── Header ────────────────────────────────────────────────────────────

    std::size_t header_len() const noexcept { return nng_msg_header_len(_msg); }
    void*       header_ptr() const noexcept { return nng_msg_header(_msg); }
    void        header_clear()     noexcept { nng_msg_header_clear(_msg); }
    int  header_append(const void* data, std::size_t len) noexcept { return nng_msg_header_append(_msg, data, len); }
    int  header_insert(const void* data, std::size_t len) noexcept { return nng_msg_header_insert(_msg, data, len); }

    // ── Dup ───────────────────────────────────────────────────────────────

    // Returns an independent copy; sets err on failure and returns empty handle.
    MessageHandle dup(int& err) const noexcept {
        nng_msg* copy = nullptr;
        err = nng_msg_dup(&copy, _msg);
        if (err != 0) return MessageHandle{};
        return MessageHandle{copy};
    }

    // ── Pipe association ──────────────────────────────────────────────────

    nng_pipe get_pipe() const noexcept { return nng_msg_get_pipe(_msg); }

    // ── Static factories ─────────────────────────────────────────────────

    // Allocate an empty message (or one pre-sized with zeroes).
    // On success returns a valid handle and sets err=0.
    // On failure returns an empty handle and sets err to the nng error code.
    static MessageHandle alloc(std::size_t size, int& err) noexcept {
        nng_msg* ptr = nullptr;
        err = nng_msg_alloc(&ptr, size);
        if (err != 0) return MessageHandle{};
        return MessageHandle{ptr};
    }

    // Allocate a message and copy data into its body.
    static MessageHandle alloc_with_data(
            const void* data, std::size_t len, int& err) noexcept {
        nng_msg* ptr = nullptr;
        err = nng_msg_alloc(&ptr, 0);
        if (err != 0) return MessageHandle{};
        if (len > 0) {
            err = nng_msg_append(ptr, data, len);
            if (err != 0) { nng_msg_free(ptr); return MessageHandle{}; }
        }
        return MessageHandle{ptr};
    }

private:
    void free_if_owned() noexcept {
        if (_msg) { nng_msg_free(_msg); _msg = nullptr; }
    }

    nng_msg* _msg;
};

} // namespace nng_cpp

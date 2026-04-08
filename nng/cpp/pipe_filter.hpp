// nng/cpp/pipe_filter.hpp
//
// GIL-free, thread-safe pipe connection filters for PipeCollection.
//
// Filters run synchronously inside PipeCollection::handle_event at ADD_PRE
// time, before the Python dispatcher is woken.  A rejected pipe never enters
// the Python-visible pipe list.
//
// Hierarchy:
//   IPipeFilter        – abstract interface
//   IpFilter           – CIDR whitelist/blacklist
//   PortFilter         – port-range whitelist/blacklist
//   PidFilter          – static PID whitelist/blacklist
//   FirstWinsFilter    – locks onto first-seen value(s) of selected key(s)
//   AllFilter          – AND of children
//   AnyFilter          – OR of children
//
// All implementations are thread-safe (guarded by internal mutexes where
// mutable state exists).  No OS calls, no new dependencies.
#pragma once

#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <vector>

#include <nng/nng.h>

namespace nng_cpp {

// ─────────────────────────────────────────────────────────────────────────────
// PipeFilterInfo - lightweight snapshot of a connecting pipe's identity.
//
// Built from a live nng_pipe at ADD_PRE time while all nng getters are valid.
// ─────────────────────────────────────────────────────────────────────────────
struct PipeFilterInfo {
    /// Peer address; family is NNG_AF_INET / NNG_AF_INET6 / NNG_AF_IPC / etc.
    nng_sockaddr peer_addr;
    /// Peer process ID from NNG_OPT_PEER_PID; -1 if not supported by transport.
    int          peer_pid;
    uint32_t     pipe_id;
    nng_dialer   dialer;    ///< {0} if inbound (listener-side)
    nng_listener listener;  ///< {0} if outbound (dialer-side)

    /// Default constructor - zero-initialises all fields.
    PipeFilterInfo() noexcept
        : peer_addr{}, peer_pid(-1), pipe_id(0), dialer{0}, listener{0} {}

    /// Build from a live pipe handle (ADD_PRE time, nng handle valid).
    // Note: this is a bit duplicated from PipeHandle's constructor, but
    // it allows clear separation of concerns.
    explicit PipeFilterInfo(nng_pipe p) noexcept
        : peer_addr{}, peer_pid(-1),
          pipe_id(static_cast<uint32_t>(nng_pipe_id(p))),
          dialer(nng_pipe_dialer(p)),
          listener(nng_pipe_listener(p))
    {
        nng_pipe_peer_addr(p, &peer_addr);
        if (nng_pipe_get_int(p, NNG_OPT_PEER_PID, &peer_pid) != 0)
            peer_pid = -1;
    }
};


// ─────────────────────────────────────────────────────────────────────────────
// IPipeFilter - abstract filter interface (thread-safe contract).
// ─────────────────────────────────────────────────────────────────────────────
class IPipeFilter {
public:
    virtual ~IPipeFilter() = default;

    /// Return false to reject the connection (nng_pipe_close is called
    /// immediately; ADD_POST will NOT fire for this pipe).
    /// Called at ADD_PRE while the nng_pipe handle is still valid.
    virtual bool allow(const PipeFilterInfo& info) noexcept = 0;

    /// Called at ADD_POST for pipes that passed allow().
    virtual void on_added(const PipeFilterInfo& /*info*/) noexcept {}

    /// Called at REM_POST for pipes that passed allow() (before removal).
    virtual void on_removed(const PipeFilterInfo& /*info*/) noexcept {}
};


// ─────────────────────────────────────────────────────────────────────────────
// Internal helpers (pure arithmetic, no OS calls, no allocations)
// ─────────────────────────────────────────────────────────────────────────────
namespace detail {

/// True if two sockaddrs represent the same IP address.
/// Non-IP families (IPC/inproc) compare equal - same-machine assumption.
inline bool addrs_equal_ip(const nng_sockaddr& a, const nng_sockaddr& b) noexcept {
    const uint16_t fa = a.s_family;
    const uint16_t fb = b.s_family;

    // Non-IP on either side -> same machine -> equal on the IP dimension.
    if (fa != NNG_AF_INET && fa != NNG_AF_INET6) return true;
    if (fb != NNG_AF_INET && fb != NNG_AF_INET6) return true;

    // Different IP versions -> not equal.
    if (fa != fb) return false;

    // IPv4: compare the 32-bit address (network byte order).
    if (fa == NNG_AF_INET)
        return a.s_in.sa_addr == b.s_in.sa_addr;

    // IPv6: compare all 16 bytes.
    return std::memcmp(a.s_in6.sa_addr, b.s_in6.sa_addr, 16) == 0;
}

/// Extract the port from a sockaddr (network byte order).
/// Returns 0 for non-IP transports.
inline uint16_t sockaddr_port(const nng_sockaddr& sa) noexcept {
    if (sa.s_family == NNG_AF_INET)  return sa.s_in.sa_port;
    if (sa.s_family == NNG_AF_INET6) return sa.s_in6.sa_port;
    return 0;
}

/// CIDR match for a 32-bit IPv4 address stored in network byte order.
/// Works byte-by-byte so that host/network byte order does not matter.
inline bool cidr4_match(uint32_t addr, uint32_t cidr, uint8_t prefix_len) noexcept {
    const auto* a = reinterpret_cast<const uint8_t*>(&addr);
    const auto* c = reinterpret_cast<const uint8_t*>(&cidr);

    // prefix_len is in bits.
    const int num_bytes = prefix_len / 8;
    const int rem_bits  = prefix_len % 8;

    // Compare prefix
    for (int i = 0; i < num_bytes; ++i)
        if (a[i] != c[i]) return false;

    // Compare remaining bits, if any.
    if (rem_bits == 0) return true;
    const uint8_t mask = static_cast<uint8_t>((0xFFu << (8 - rem_bits)) & 0xFFu);
    return (a[num_bytes] & mask) == (c[num_bytes] & mask);
}

/// CIDR match for a 128-bit IPv6 address (16 raw bytes, big-endian).
inline bool cidr6_match(const uint8_t* addr,
                        const uint8_t* cidr,
                        uint8_t        prefix_len) noexcept {
    // prefix_len is in bits.
    const int num_bytes = prefix_len / 8;
    const int rem_bits  = prefix_len % 8;

    // Compare prefix
    for (int i = 0; i < num_bytes; ++i)
        if (addr[i] != cidr[i]) return false;

    // Compare remaining bits, if any.
    if (rem_bits == 0) return true;
    const uint8_t mask = static_cast<uint8_t>((0xFFu << (8 - rem_bits)) & 0xFFu);
    return (addr[num_bytes] & mask) == (cidr[num_bytes] & mask);
}

} // namespace detail


// ─────────────────────────────────────────────────────────────────────────────
// FilterMode - whether the address/PID/port list is a whitelist or blacklist.
// ─────────────────────────────────────────────────────────────────────────────
enum class FilterMode : int { ALLOW = 0, DENY = 1 };


// ─────────────────────────────────────────────────────────────────────────────
// IpFilter - CIDR whitelist / blacklist.
// ─────────────────────────────────────────────────────────────────────────────
struct Cidr4Entry { uint32_t addr; uint8_t prefix_len; };
struct Cidr6Entry { uint8_t  addr[16]; uint8_t prefix_len; };

/// Set the 16-byte addr field of a Cidr6Entry from a raw byte pointer.
inline void cidr6_entry_set_addr(Cidr6Entry& e, const uint8_t* data) noexcept {
    std::memcpy(e.addr, data, 16);
}

class IpFilter final : public IPipeFilter {
public:
    IpFilter(FilterMode mode,
             std::vector<Cidr4Entry> v4,
             std::vector<Cidr6Entry> v6) noexcept
        : _mode(mode),
          _v4(std::move(v4)), _v6(std::move(v6)) {}

    bool allow(const PipeFilterInfo& info) noexcept override {
        const uint16_t family = info.peer_addr.s_family;
        if (family != NNG_AF_INET && family != NNG_AF_INET6)
            return true; // pass for non-IP transport
        const bool matched = _matches(info.peer_addr, family);
        return (_mode == FilterMode::ALLOW) ? matched : !matched;
    }

private:
    bool _matches(const nng_sockaddr& sa, uint16_t family) const noexcept {
        if (family == NNG_AF_INET) {
            for (const auto& e : _v4)
                if (detail::cidr4_match(sa.s_in.sa_addr, e.addr, e.prefix_len))
                    return true;
            return false;
        }
        if (family == NNG_AF_INET6) {
            // IPv6: sa_addr is uint8_t[16].
            for (const auto& e : _v6)
                if (detail::cidr6_match(sa.s_in6.sa_addr, e.addr, e.prefix_len))
                    return true;
        }
        return false;
    }

    FilterMode              _mode;
    std::vector<Cidr4Entry> _v4;
    std::vector<Cidr6Entry> _v6;
};


// ─────────────────────────────────────────────────────────────────────────────
// PortFilter - port-range whitelist / blacklist.
// ─────────────────────────────────────────────────────────────────────────────
struct PortRange { uint16_t lo; uint16_t hi; };

class PortFilter final : public IPipeFilter {
public:
    PortFilter(FilterMode mode, std::vector<PortRange> ranges) noexcept
        : _mode(mode), _ranges(std::move(ranges)) {}

    bool allow(const PipeFilterInfo& info) noexcept override {
        const uint16_t port = detail::sockaddr_port(info.peer_addr);

        if (port == 0)
            return true; // pass for non-IP transport

        bool matched = false;
        for (const auto& r : _ranges)
            if (port >= r.lo && port <= r.hi) { matched = true; break; }
        return (_mode == FilterMode::ALLOW) ? matched : !matched;
    }

private:
    FilterMode             _mode;
    std::vector<PortRange> _ranges;
};


// ─────────────────────────────────────────────────────────────────────────────
// PidFilter - static PID whitelist / blacklist.
// ─────────────────────────────────────────────────────────────────────────────
class PidFilter final : public IPipeFilter {
public:
    PidFilter(FilterMode mode, std::vector<int> pids) noexcept
        : _mode(mode), _pids(std::move(pids)) {}

    bool allow(const PipeFilterInfo& info) noexcept override {
        if (info.peer_pid == -1)
            return true; // pass for non-IPC transport

        bool matched = false;
        for (int p : _pids)
            if (info.peer_pid == p) { matched = true; break; }
        return (_mode == FilterMode::ALLOW) ? matched : !matched;
    }

private:
    FilterMode       _mode;
    std::vector<int> _pids;
};


// ─────────────────────────────────────────────────────────────────────────────
// FilterKey - bitmask enum for FirstWinsFilter key selection.
// ─────────────────────────────────────────────────────────────────────────────
enum class FilterKey : int {
    PID  = 1,  ///< Match on peer PID  (TCP: pid==-1 so all -1s compare equal)
    IP   = 2,  ///< Match on peer IP   (non-IP transports: all compare equal)
    PORT = 4,  ///< Match on peer port (non-IP transports: port==0, all equal)
};

inline constexpr int operator|(FilterKey a, FilterKey b) noexcept {
    return static_cast<int>(a) | static_cast<int>(b);
}
inline constexpr int operator|(int a, FilterKey b) noexcept {
    return a | static_cast<int>(b);
}


// ─────────────────────────────────────────────────────────────────────────────
// FirstWinsFilter - locks onto the first-seen value(s) of selected key(s).
//
// key_mask is an OR-combination of FilterKey values.  ALL selected dimensions
// must match after the anchor is established (AND semantics on values).
//
// Transport edge-case semantics:
//   FilterKey::IP   on non-IP (IPC/inproc): non-IP addrs compare equal
//                   (same-machine), so the IP dimension never rejects.
//   FilterKey::PID  on TCP (peer_pid==-1):  -1 == -1 always matches.
//   FilterKey::PORT on non-IP:              port is 0 for all, always equal.
//
// Thread-safe.
// ─────────────────────────────────────────────────────────────────────────────
class FirstWinsFilter final : public IPipeFilter {
public:
    explicit FirstWinsFilter(int key_mask) noexcept
        : _key_mask(key_mask), _initialized(false),
          _first_pid(-1), _first_addr{}, _first_port(0) {}

    bool allow(const PipeFilterInfo& info) noexcept override {
        std::lock_guard<std::mutex> lock(_mutex);

        // First connection: store anchor values for all selected keys.
        if (!_initialized) {
            if (_key_mask & static_cast<int>(FilterKey::PID))
                _first_pid  = info.peer_pid;
            if (_key_mask & static_cast<int>(FilterKey::IP))
                _first_addr = info.peer_addr;
            if (_key_mask & static_cast<int>(FilterKey::PORT))
                _first_port = detail::sockaddr_port(info.peer_addr);
            _initialized = true;
            return true;
        }

        // Subsequent connections: check each selected dimension.
        if (_key_mask & static_cast<int>(FilterKey::PID))
            if (info.peer_pid != _first_pid) return false;
        if (_key_mask & static_cast<int>(FilterKey::IP))
            if (!detail::addrs_equal_ip(info.peer_addr, _first_addr)) return false;
        if (_key_mask & static_cast<int>(FilterKey::PORT))
            if (detail::sockaddr_port(info.peer_addr) != _first_port) return false;
        return true;
    }

    /// Release the anchor so the next connection becomes the new first.
    void reset() noexcept {
        std::lock_guard<std::mutex> lock(_mutex);
        _initialized = false;
    }

private:
    std::mutex   _mutex;
    int          _key_mask;
    bool         _initialized;
    int          _first_pid;
    nng_sockaddr _first_addr;
    uint16_t     _first_port;
};


// ─────────────────────────────────────────────────────────────────────────────
// AllFilter / AnyFilter - composite logic.
// ─────────────────────────────────────────────────────────────────────────────
class AllFilter final : public IPipeFilter {
public:
    explicit AllFilter(std::vector<std::shared_ptr<IPipeFilter>> children) noexcept
        : _children(std::move(children)) {}

    bool allow(const PipeFilterInfo& info) noexcept override {
        for (const auto& c : _children)
            if (!c->allow(info)) return false;
        return true;
    }
    void on_added(const PipeFilterInfo& info) noexcept override {
        for (const auto& c : _children) c->on_added(info);
    }
    void on_removed(const PipeFilterInfo& info) noexcept override {
        for (const auto& c : _children) c->on_removed(info);
    }

private:
    std::vector<std::shared_ptr<IPipeFilter>> _children;
};

class AnyFilter final : public IPipeFilter {
public:
    explicit AnyFilter(std::vector<std::shared_ptr<IPipeFilter>> children) noexcept
        : _children(std::move(children)) {}

    bool allow(const PipeFilterInfo& info) noexcept override {
        for (const auto& c : _children)
            if (c->allow(info)) return true;
        return false;
    }
    void on_added(const PipeFilterInfo& info) noexcept override {
        for (const auto& c : _children) c->on_added(info);
    }
    void on_removed(const PipeFilterInfo& info) noexcept override {
        for (const auto& c : _children) c->on_removed(info);
    }

private:
    std::vector<std::shared_ptr<IPipeFilter>> _children;
};


// ─────────────────────────────────────────────────────────────────────────────
// Factory functions (Cython-compatible: plain free functions, no templates).
// ─────────────────────────────────────────────────────────────────────────────

inline std::shared_ptr<IPipeFilter>
make_all_filter(std::vector<std::shared_ptr<IPipeFilter>> v) {
    return std::make_shared<AllFilter>(std::move(v));
}

inline std::shared_ptr<IPipeFilter>
make_any_filter(std::vector<std::shared_ptr<IPipeFilter>> v) {
    return std::make_shared<AnyFilter>(std::move(v));
}

inline std::shared_ptr<IPipeFilter>
make_ip_filter(FilterMode mode,
               std::vector<Cidr4Entry> v4,
               std::vector<Cidr6Entry> v6) {
    return std::make_shared<IpFilter>(
        mode,
        std::move(v4),
        std::move(v6)
    );
}

inline std::shared_ptr<IPipeFilter>
make_port_filter(FilterMode mode, std::vector<PortRange> ranges) {
    return std::make_shared<PortFilter>(mode, std::move(ranges));
}

inline std::shared_ptr<IPipeFilter>
make_pid_filter(FilterMode mode, std::vector<int> pids) {
    return std::make_shared<PidFilter>(mode, std::move(pids));
}

inline std::shared_ptr<IPipeFilter>
make_first_wins_filter(int key_mask) {
    return std::make_shared<FirstWinsFilter>(key_mask);
}


} // namespace nng_cpp

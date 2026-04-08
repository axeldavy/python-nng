# nng/_pipe_filter.pxi – included into _nng.pyx
#
# PipeFilter   – base extension type wrapping shared_ptr[IPipeFilter]
# FilterMode   – IntEnum: ALLOW / DENY
# FilterKey    – IntFlag: PID / IP / PORT (bitmask)
# IpFilter     – CIDR whitelist / blacklist
# PortFilter   – port-range whitelist / blacklist
# PidFilter    – static PID whitelist / blacklist
# FirstWinsFilter – locks onto first-seen key dimension values

import enum as _enum
import ipaddress as _ipaddress

from libcpp.memory cimport shared_ptr
from libcpp.utility cimport move
from libcpp.vector cimport vector
from libc.string cimport memcpy


# ── Public enums ──────────────────────────────────────────────────────────────

class FilterMode(_enum.IntEnum):
    """Whether a filter's address/PID/port list acts as a whitelist or blacklist.

    * ``ALLOW`` – only connections matching the list are accepted.
    * ``DENY``  – connections matching the list are rejected; all others pass.
    """

    ALLOW = 0
    DENY  = 1


class FilterKey(_enum.IntFlag):
    """Key dimensions used by :class:`FirstWinsFilter`.

    Values are powers of two and may be combined with ``|``::

        FirstWinsFilter(on=FilterKey.PID | FilterKey.IP)

    * ``PID``  – match on peer process ID.
    * ``IP``   – match on peer IP address.
    * ``PORT`` – match on peer TCP port.
    """

    PID  = 1
    IP   = 2
    PORT = 4


# ── Base PipeFilter extension type ───────────────────────────────────────────

cdef class PipeFilter:
    """Base class for all pipe connection filters.

    Cannot be instantiated directly; use a concrete subclass such as
    :class:`IpFilter`, :class:`PidFilter`, :class:`PortFilter`, or
    :class:`FirstWinsFilter`.

    Filters can be composed with Python operators::

        f = IpFilter(["10.0.0.0/8"], mode=FilterMode.ALLOW) & PidFilter([1234])
        g = IpFilter(["192.168.0.0/16"]) | IpFilter(["10.0.0.0/8"])

    The composed result is also a :class:`PipeFilter` and can be assigned to
    :attr:`Socket.pipe_filter`.
    """

    cdef shared_ptr[IPipeFilter] _impl

    def __init__(self):
        """Raise TypeError — PipeFilter cannot be instantiated directly."""
        raise TypeError("PipeFilter cannot be instantiated directly; use a subclass")

    def __and__(self, other):
        """Return a filter that passes only when both *self* and *other* pass (AND)."""
        if not isinstance(other, PipeFilter):
            return NotImplemented
        cdef vector[shared_ptr[IPipeFilter]] children
        children.push_back((<PipeFilter>self)._impl)
        children.push_back((<PipeFilter>other)._impl)
        cdef PipeFilter result = PipeFilter.__new__(PipeFilter)
        result._impl = make_all_filter(move(children))
        return result

    def __or__(self, other):
        """Return a filter that passes when either *self* or *other* passes (OR)."""
        if not isinstance(other, PipeFilter):
            return NotImplemented
        cdef vector[shared_ptr[IPipeFilter]] children
        children.push_back((<PipeFilter>self)._impl)
        children.push_back((<PipeFilter>other)._impl)
        cdef PipeFilter result = PipeFilter.__new__(PipeFilter)
        result._impl = make_any_filter(move(children))
        return result


# ── IpFilter ──────────────────────────────────────────────────────────────────

cdef class IpFilter(PipeFilter):
    """CIDR-based IP whitelist or blacklist.

    *addresses* is a list of strings in CIDR notation (``"10.0.0.0/8"``,
    ``"192.168.1.5/32"``, ``"2001:db8::/32"``).  Plain addresses without a
    prefix length are treated as host routes (``/32`` for IPv4, ``/128`` for
    IPv6).

    Non-IP transports (IPC/inproc) do not have a peer IP address.  They always
    pass this filter regardless of *mode* (same-machine assumption).

    Parameters
    ----------
    addresses:
        Iterable of CIDR strings or :class:`ipaddress.IPv4Network` /
        :class:`ipaddress.IPv6Network` objects.
    mode:
        :attr:`FilterMode.ALLOW` (whitelist) or :attr:`FilterMode.DENY`
        (blacklist).  Default: ``FilterMode.ALLOW``.

    Raises
    ------
    ValueError
        If any entry in *addresses* is not a valid network.
    """

    def __init__(
        self,
        addresses,
        *,
        mode: FilterMode = FilterMode.ALLOW
    ) -> None:
        """Initialize IpFilter with CIDR addresses, filter mode, and non-IP policy."""
        cdef vector[Cidr4Entry] v4
        cdef vector[Cidr6Entry] v6
        cdef Cidr4Entry e4
        cdef Cidr6Entry e6
        cdef const unsigned char* raw

        # Parse each address or network.
        for entry in addresses:
            net = _ipaddress.ip_network(entry, strict=False)
            packed = net.network_address.packed

            if isinstance(net, _ipaddress.IPv4Network):
                # Store IPv4 address in network byte order (same as nng sa_addr).
                raw = <const unsigned char*><const char*>packed
                memcpy(&e4.addr, raw, 4)
                e4.prefix_len = net.prefixlen
                v4.push_back(e4)
            else:
                # IPv6: 16 bytes in network byte order.
                raw = <const unsigned char*><const char*>packed
                cidr6_entry_set_addr(e6, <const uint8_t*>raw)
                e6.prefix_len = net.prefixlen
                v6.push_back(e6)

        cdef CppFilterMode cpp_mode = (
            CPP_FILTER_ALLOW if mode == FilterMode.ALLOW else CPP_FILTER_DENY
        )
        self._impl = make_ip_filter(cpp_mode, v4, v6)


# ── PortFilter ────────────────────────────────────────────────────────────────

cdef class PortFilter(PipeFilter):
    """Port-range whitelist or blacklist.

    *ports* is an iterable of port specifications: each element is either
    a plain :class:`int` (single port) or a ``(lo, hi)`` tuple (inclusive
    range).  Ports are in the range 0–65535.

    Note: non-IP transports do not have a peer port. They pass the
    filter for both ``ALLOW`` and ``DENY`` modes.

    Parameters
    ----------
    ports:
        Iterable of port numbers or ``(low, high)`` inclusive ranges.
    mode:
        :attr:`FilterMode.ALLOW` (whitelist) or :attr:`FilterMode.DENY`
        (blacklist).  Default: ``FilterMode.ALLOW``.

    Raises
    ------
    ValueError
        If any port value is outside 0–65535.
    TypeError
        If any element is neither an int nor a two-element tuple of int.
    """

    def __init__(self, ports, *, mode: FilterMode = FilterMode.ALLOW) -> None:
        """Initialize PortFilter with port ranges and filter mode."""
        cdef vector[PortRange] ranges
        cdef PortRange r

        for entry in ports:
            if isinstance(entry, int):
                lo = hi = int(entry)
            else:
                lo, hi = int(entry[0]), int(entry[1])
            if not (0 <= lo <= 65535) or not (0 <= hi <= 65535):
                raise ValueError(f"Port out of range 0-65535: {entry!r}")
            r.lo = lo
            r.hi = hi
            ranges.push_back(r)

        cdef CppFilterMode cpp_mode = CPP_FILTER_ALLOW if mode == FilterMode.ALLOW else CPP_FILTER_DENY
        self._impl = make_port_filter(cpp_mode, ranges)


# ── PidFilter ─────────────────────────────────────────────────────────────────

cdef class PidFilter(PipeFilter):
    """Process-ID whitelist or blacklist.

    Useful only on transports that report the peer PID, namely IPC
    (``ipc://``).  On TCP and inproc the peer PID is always
    ``-1`` and pass the filter for both ``ALLOW`` and ``DENY`` modes.

    Parameters
    ----------
    pids:
        Iterable of integer process IDs.
    mode:
        :attr:`FilterMode.ALLOW` (whitelist) or :attr:`FilterMode.DENY`
        (blacklist).  Default: ``FilterMode.ALLOW``.
    """

    def __init__(self, pids, *, mode: FilterMode = FilterMode.ALLOW) -> None:
        """Initialize PidFilter with a list of PIDs and filter mode."""
        cdef vector[int] pid_list
        for p in pids:
            pid_list.push_back(int(p))

        cdef CppFilterMode cpp_mode = CPP_FILTER_ALLOW if mode == FilterMode.ALLOW else CPP_FILTER_DENY
        self._impl = make_pid_filter(cpp_mode, pid_list)


# ── FirstWinsFilter ───────────────────────────────────────────────────────────

cdef class FirstWinsFilter(PipeFilter):
    """Lock onto the first-seen value(s) of the selected key dimension(s).

    The first connection to pass establishes the anchor values for all
    selected keys.  Every subsequent connection is accepted only if ALL
    selected dimensions match the anchor (AND semantics).

    *on* is a :class:`FilterKey` flag (or OR-combination of flags):

    * ``FilterKey.PID``  – lock on the peer process ID.
    * ``FilterKey.IP``   – lock on the peer IP address.
    * ``FilterKey.PORT`` – lock on the peer source port.

    **Transport edge cases**

    ========== ============================================================
    Key        Behaviour when transport does not expose the dimension
    ========== ============================================================
    ``IP``     Non-IP transports (IPC/inproc): all compare equal
               (same-machine assumption) → never rejects on the IP dimension.
    ``PID``    IP transports have ``pid == -1``; ``-1 == -1`` always
               matches, so the PID dimension never rejects TCP connections.
    ``PORT``   Non-IP transports have ``port == 0``; always equal.
    ========== ============================================================

    Parameters
    ----------
    on:
        Key dimension(s) to lock on.  Default: ``FilterKey.PID | FilterKey.IP | FilterKey.PORT``.

    Raises
    ------
    ValueError
        If *on* contains no valid ``FilterKey`` bits.
    """

    def __init__(self, *, on: FilterKey = FilterKey.PID | FilterKey.IP | FilterKey.PORT) -> None:
        """Initialize FirstWinsFilter for the given key dimension(s)."""
        cdef int key_mask = int(on)
        if key_mask == 0:
            raise ValueError("on must include at least one FilterKey bit")

        self._impl = make_first_wins_filter(key_mask)

    def reset(self) -> None:
        """Release the anchor so the next connection becomes the new first.

        After calling :meth:`reset`, the next connecting peer establishes a
        new anchor.  Existing connections are not affected.
        """
        (<CppFirstWinsFilter*>self._impl.get()).reset()

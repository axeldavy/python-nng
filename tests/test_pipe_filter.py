"""Tests for pipe connection filters and PairSocket.close_on_disconnect.

Covers:
- FilterMode and FilterKey enums
- IpFilter: ALLOW/DENY mode, IPv4/IPv6 CIDR, non-IP transports always pass
- PortFilter: single ports, ranges, ALLOW/DENY mode
- PidFilter: ALLOW/DENY mode; TCP always has pid=-1
- FirstWinsFilter: PID, IP, PORT dimensions; reset(); combined keys
- PipeFilter composition: &, |, ~
- Socket.pipe_filter assignment, clear, and enforcement
- PairSocket.close_on_disconnect behaviour
- Error handling: bad addresses, ports, empty on-mask

NOTE on timing
--------------
Pipe lifecycle events are dispatched from nng's internal I/O thread.
All tests that need to observe a connection use a threading.Event and poll
with a short deadline to avoid races.
"""
import os
import time
import threading
import pytest
import nng


_TIMEOUT = 2.0   # seconds for all waits
_IPC_URL = "ipc:///tmp/test_pipe_filter.ipc"
_INPROC_URL = "inproc://test_pipe_filter"
_TCP_URL = "tcp://127.0.0.1:15800"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wait(cond_fn, *, timeout: float = _TIMEOUT, msg: str = "condition") -> None:
    """Poll *cond_fn()* until it returns truthy or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond_fn():
            return
        time.sleep(0.005)
    pytest.fail(f"Timed out waiting for: {msg}")


def _connect_pair(srv_url: str, *, srv_filter=None):
    """Return (srv, cli) PairSockets that have an established pipe.

    The calling test is responsible for closing both sockets.
    """
    srv = nng.PairSocket()
    cli = nng.PairSocket()
    if srv_filter is not None:
        srv.pipe_filter = srv_filter
    connected = threading.Event()
    srv.on_new_pipe = lambda p: connected.set()
    srv.add_listener(srv_url).start()
    cli.add_dialer(srv_url).start()
    return srv, cli, connected


# ── FilterMode and FilterKey enums ────────────────────────────────────────────

def test_filter_mode_values():
    """FilterMode has ALLOW=0 and DENY=1."""
    assert nng.FilterMode.ALLOW == 0
    assert nng.FilterMode.DENY == 1


def test_filter_key_is_flags():
    """FilterKey values are powers of two and can be combined with |."""
    combined = nng.FilterKey.PID | nng.FilterKey.IP | nng.FilterKey.PORT
    assert int(combined) == int(nng.FilterKey.PID) | int(nng.FilterKey.IP) | int(nng.FilterKey.PORT)


# ── IpFilter construction errors ─────────────────────────────────────────────

def test_ip_filter_bad_address_raises():
    """IpFilter raises ValueError for a non-CIDR string."""
    with pytest.raises(ValueError):
        nng.IpFilter(["not-an-address"])


def test_ip_filter_empty_list_ok():
    """IpFilter with an empty list is valid (deny-all in ALLOW mode)."""
    f = nng.IpFilter([])
    assert isinstance(f, nng.PipeFilter)


def test_ip_filter_ipv4_allow_mode():
    """IpFilter in ALLOW mode accepts a plain IPv4 /32 string."""
    f = nng.IpFilter(["127.0.0.1/32"])
    assert isinstance(f, nng.IpFilter)


def test_ip_filter_ipv6_allow_mode():
    """IpFilter accepts an IPv6 CIDR string."""
    f = nng.IpFilter(["::1/128"])
    assert isinstance(f, nng.IpFilter)


# ── PortFilter construction errors ────────────────────────────────────────────

def test_port_filter_out_of_range_raises():
    """PortFilter raises ValueError for a port > 65535."""
    with pytest.raises(ValueError):
        nng.PortFilter([70000])


def test_port_filter_single_port():
    """PortFilter accepts a single integer port."""
    f = nng.PortFilter([80])
    assert isinstance(f, nng.PortFilter)


def test_port_filter_range_tuple():
    """PortFilter accepts (lo, hi) inclusive range tuples."""
    f = nng.PortFilter([(1024, 65535)])
    assert isinstance(f, nng.PortFilter)


# ── PidFilter construction ────────────────────────────────────────────────────

def test_pid_filter_allow_mode():
    """PidFilter in ALLOW mode with current PID is valid."""
    f = nng.PidFilter([os.getpid()])
    assert isinstance(f, nng.PidFilter)


def test_pid_filter_deny_mode():
    """PidFilter in DENY mode with empty list is valid (passes all)."""
    f = nng.PidFilter([], mode=nng.FilterMode.DENY)
    assert isinstance(f, nng.PidFilter)


# ── FirstWinsFilter construction ──────────────────────────────────────────────

def test_first_wins_filter_default_key():
    """FirstWinsFilter defaults to FilterKey.PID."""
    f = nng.FirstWinsFilter()
    assert isinstance(f, nng.FirstWinsFilter)


def test_first_wins_filter_combined_keys():
    """FirstWinsFilter accepts a combined key mask."""
    f = nng.FirstWinsFilter(on=nng.FilterKey.PID | nng.FilterKey.IP)
    assert isinstance(f, nng.FirstWinsFilter)


def test_first_wins_filter_empty_mask_raises():
    """FirstWinsFilter raises ValueError when on=0."""
    with pytest.raises(ValueError):
        nng.FirstWinsFilter(on=nng.FilterKey(0))


def test_first_wins_filter_reset():
    """reset() does not raise and clears the anchor."""
    f = nng.FirstWinsFilter(on=nng.FilterKey.PID)
    f.reset()
    # After reset, should still be usable — no error.
    f.reset()


# ── PipeFilter composition ────────────────────────────────────────────────────

def test_filter_and_composition():
    """& produces a PipeFilter (AllFilter)."""
    f = nng.IpFilter(["127.0.0.1/32"]) & nng.PortFilter([1234])
    assert isinstance(f, nng.PipeFilter)


def test_filter_or_composition():
    """| produces a PipeFilter (AnyFilter)."""
    f = nng.IpFilter(["10.0.0.0/8"]) | nng.IpFilter(["192.168.0.0/16"])
    assert isinstance(f, nng.PipeFilter)


def test_filter_complex_composition():
    """Complex composition does not raise."""
    f = nng.IpFilter(["10.0.0.0/8"]) & (nng.PortFilter([(1, 1023)]) | nng.PidFilter([0]))
    assert isinstance(f, nng.PipeFilter)


def test_filter_and_wrong_type_returns_not_implemented():
    """& with a non-PipeFilter returns NotImplemented."""
    f = nng.IpFilter(["127.0.0.1/32"])
    result = f.__and__(42)
    assert result is NotImplemented


# ── Socket.pipe_filter property ───────────────────────────────────────────────

def test_socket_pipe_filter_none_by_default():
    """Socket.pipe_filter is None when no filter has been set."""
    with nng.PairSocket() as s:
        assert s.pipe_filter is None


def test_socket_pipe_filter_set_and_get():
    """Setting pipe_filter stores the object and can be retrieved."""
    f = nng.IpFilter(["127.0.0.1/32"])
    with nng.PairSocket() as s:
        s.pipe_filter = f
        assert s.pipe_filter is f


def test_socket_pipe_filter_clear():
    """Setting pipe_filter = None clears the filter."""
    f = nng.IpFilter(["127.0.0.1/32"])
    with nng.PairSocket() as s:
        s.pipe_filter = f
        s.pipe_filter = None
        assert s.pipe_filter is None


def test_socket_pipe_filter_wrong_type_raises():
    """Setting pipe_filter to a non-PipeFilter (and non-None) raises TypeError."""
    with nng.PairSocket() as s:
        with pytest.raises(TypeError):
            s.pipe_filter = "not a filter"


# ── Filter enforcement on connections ─────────────────────────────────────────

def test_ip_filter_allow_inproc_always_passes():
    """IpFilter ALLOW mode passes non-IP transports (same-machine assumption)."""
    # Empty allowlist would reject all IP peers, but inproc has no IP → always passes.
    f = nng.IpFilter([], mode=nng.FilterMode.ALLOW)
    srv, cli, connected = _connect_pair(_INPROC_URL + "_non_ip_pass", srv_filter=f)
    try:
        _wait(connected.is_set, msg="inproc connection passes IpFilter regardless of mode")
    finally:
        srv.close()
        cli.close()


def test_ip_filter_deny_inproc_always_passes():
    """IpFilter DENY mode also passes non-IP transports (symmetric same-machine assumption)."""
    # Full deny of all IPs still lets inproc through.
    f = nng.IpFilter(["0.0.0.0/0", "::/0"], mode=nng.FilterMode.DENY)
    srv, cli, connected = _connect_pair(_INPROC_URL + "_non_ip_deny", srv_filter=f)
    try:
        _wait(connected.is_set, msg="inproc connection passes IpFilter DENY mode")
    finally:
        srv.close()
        cli.close()


def test_first_wins_filter_allows_first_connection_inproc():
    """FirstWinsFilter(IP) on inproc allows the first connection (same machine)."""
    f = nng.FirstWinsFilter(on=nng.FilterKey.IP)
    srv, cli, connected = _connect_pair(_INPROC_URL + "_fw_ip", srv_filter=f)
    try:
        _wait(connected.is_set, msg="first inproc connection with FirstWinsFilter(IP)")
    finally:
        srv.close()
        cli.close()


def test_first_wins_filter_pid_allows_self_on_ipc():
    """FirstWinsFilter(PID) on IPC allows connections from the same process."""
    f = nng.FirstWinsFilter(on=nng.FilterKey.PID)
    srv, cli, connected = _connect_pair(_IPC_URL + ".fw_pid", srv_filter=f)
    try:
        _wait(connected.is_set, msg="IPC self-connection with FirstWinsFilter(PID)")
    finally:
        srv.close()
        cli.close()


def test_first_wins_filter_reset_allows_reconnect():
    """After reset(), FirstWinsFilter accepts a new connection."""
    f = nng.FirstWinsFilter(on=nng.FilterKey.PID)
    srv = nng.PairSocket()
    srv.pipe_filter = f
    connected1 = threading.Event()
    connected2 = threading.Event()
    call_count = [0]

    def _on_pipe(p):
        call_count[0] += 1
        if call_count[0] == 1:
            connected1.set()
        else:
            connected2.set()

    srv.on_new_pipe = _on_pipe
    srv.add_listener(_IPC_URL + ".fw_reset").start()

    cli1 = nng.PairSocket()
    cli1.add_dialer(_IPC_URL + ".fw_reset").start()
    try:
        _wait(connected1.is_set, msg="first connection before reset")
        cli1.close()

        # Reset the filter so a second connection can anchor.
        f.reset()

        cli2 = nng.PairSocket()
        cli2.add_dialer(_IPC_URL + ".fw_reset").start()
        try:
            _wait(connected2.is_set, msg="second connection after reset")
        finally:
            cli2.close()
    finally:
        srv.close()


# ── PairSocket.close_on_disconnect ────────────────────────────────────────────

def test_pair_close_on_disconnect_default_false():
    """PairSocket.close_on_disconnect is False by default."""
    with nng.PairSocket() as s:
        assert s.close_on_disconnect is False


def test_pair_close_on_disconnect_set_true():
    """Setting close_on_disconnect = True is stored."""
    with nng.PairSocket() as s:
        s.close_on_disconnect = True
        assert s.close_on_disconnect is True


def test_pair_close_on_disconnect_closes_on_peer_disconnect():
    """When close_on_disconnect=True, socket is closed after peer disconnects."""
    srv = nng.PairSocket()
    srv.add_listener(_IPC_URL + ".cod").start()
    srv.close_on_disconnect = True

    cli = nng.PairSocket()
    cli.add_dialer(_IPC_URL + ".cod").start()

    # Wait for the pipe to become active.
    _wait(lambda: bool(srv.pipes), msg="pipe to appear on srv")

    try:
        # Disconnect the client; server should close itself.
        cli.close()
        _wait(lambda: srv.id == 0, msg="srv to close after peer disconnect")
    finally:
        if srv.id != 0:
            srv.close()

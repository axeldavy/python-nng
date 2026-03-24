"""Tests for tcp_nodelay and tcp_keepalive options on Dialer, Listener, and Pipe.

Covers:
- Default values on TCP dialers and listeners (nodelay=True, keepalive=False).
- Setting options explicitly at creation via add_dialer / add_listener.
- Reading back set values on Dialer and Listener objects.
- Pipe inherits the TCP option values captured at connection time.
- Non-TCP transports (inproc): Dialer and Listener raise NngNotSupported;
  Pipe returns C++ defaults: tcp_nodelay=True, tcp_keepalive=False.
"""
import time

import pytest

import nng

_TIMEOUT = 2.0


def _wait_for_active_pipes(sock: nng.Socket, n: int = 1, timeout: float = _TIMEOUT) -> list[nng.Pipe]:
    """Poll until *sock* has at least *n* ACTIVE pipes, then return them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = [p for p in sock.pipes if p.status == nng.PipeStatus.ACTIVE]
        if len(active) >= n:
            return active
        time.sleep(0.005)
    pytest.fail(f"Timed out waiting for {n} ACTIVE pipe(s) on {sock!r}")


# ── TCP: default values ───────────────────────────────────────────────────────

def test_tcp_nodelay_default_true_on_dialer() -> None:
    """Dialer tcp_nodelay is True by default (Nagle disabled)."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        d = cli.add_dialer(f"tcp://127.0.0.1:{lst.port}")
        assert d.tcp_nodelay is True


def test_tcp_keepalive_default_false_on_dialer() -> None:
    """Dialer tcp_keepalive is False by default."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        d = cli.add_dialer(f"tcp://127.0.0.1:{lst.port}")
        assert d.tcp_keepalive is False


def test_tcp_nodelay_default_true_on_listener() -> None:
    """Listener tcp_nodelay is True by default (Nagle disabled)."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        assert lst.tcp_nodelay is True


def test_tcp_keepalive_default_false_on_listener() -> None:
    """Listener tcp_keepalive is False by default."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        assert lst.tcp_keepalive is False


# ── TCP: setting options at creation time ─────────────────────────────────────

def test_set_tcp_nodelay_false_on_dialer() -> None:
    """Setting tcp_nodelay=False at dialer creation is readable back."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        d = cli.add_dialer(f"tcp://127.0.0.1:{lst.port}", tcp_nodelay=False)
        assert d.tcp_nodelay is False


def test_set_tcp_nodelay_true_on_dialer() -> None:
    """Setting tcp_nodelay=True at dialer creation is readable back."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        d = cli.add_dialer(f"tcp://127.0.0.1:{lst.port}", tcp_nodelay=True)
        assert d.tcp_nodelay is True


def test_set_tcp_keepalive_true_on_dialer() -> None:
    """Setting tcp_keepalive=True at dialer creation is readable back."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        d = cli.add_dialer(f"tcp://127.0.0.1:{lst.port}", tcp_keepalive=True)
        assert d.tcp_keepalive is True


def test_set_tcp_nodelay_false_on_listener() -> None:
    """Setting tcp_nodelay=False at listener creation is readable back."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("tcp://127.0.0.1:0", tcp_nodelay=False)
        lst.start()
        assert lst.tcp_nodelay is False


def test_set_tcp_nodelay_true_on_listener() -> None:
    """Setting tcp_nodelay=True at listener creation is readable back."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("tcp://127.0.0.1:0", tcp_nodelay=True)
        lst.start()
        assert lst.tcp_nodelay is True


def test_set_tcp_keepalive_true_on_listener() -> None:
    """Setting tcp_keepalive=True at listener creation is readable back."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("tcp://127.0.0.1:0", tcp_keepalive=True)
        lst.start()
        assert lst.tcp_keepalive is True


# ── TCP: pipe inherits option values captured at connection time ───────────────

def test_pipe_tcp_nodelay_false_from_dialer() -> None:
    """Dialer-side pipe reflects tcp_nodelay=False set on the dialer."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}", tcp_nodelay=False).start()

        # The dialer-side pipe is on cli.
        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.tcp_nodelay is False


def test_pipe_tcp_keepalive_true_from_dialer() -> None:
    """Dialer-side pipe reflects tcp_keepalive=True set on the dialer."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}", tcp_keepalive=True).start()

        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.tcp_keepalive is True


def test_pipe_tcp_nodelay_false_from_listener() -> None:
    """Listener-side (server) pipe reflects tcp_nodelay=False set on the listener."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0", tcp_nodelay=False)
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}").start()

        # The listener-side pipe is on srv.
        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.tcp_nodelay is False


def test_pipe_tcp_keepalive_true_from_listener() -> None:
    """Listener-side (server) pipe reflects tcp_keepalive=True set on the listener."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0", tcp_keepalive=True)
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}").start()

        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.tcp_keepalive is True


# ── Non-TCP (inproc): Dialer and Listener raise; Pipe returns defaults ─────────

def test_tcp_nodelay_raises_on_inproc_dialer() -> None:
    """tcp_nodelay on an inproc dialer raises NngNotSupported."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tcp_opts_nd_dialer").start()
        d = cli.add_dialer("inproc://test_tcp_opts_nd_dialer")
        with pytest.raises(nng.NngNotSupported):
            _ = d.tcp_nodelay


def test_tcp_keepalive_raises_on_inproc_dialer() -> None:
    """tcp_keepalive on an inproc dialer raises NngNotSupported."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tcp_opts_ka_dialer").start()
        d = cli.add_dialer("inproc://test_tcp_opts_ka_dialer")
        with pytest.raises(nng.NngNotSupported):
            _ = d.tcp_keepalive


def test_tcp_nodelay_raises_on_inproc_listener() -> None:
    """tcp_nodelay on an inproc listener raises NngNotSupported."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("inproc://test_tcp_opts_nd_listener")
        with pytest.raises(nng.NngNotSupported):
            _ = lst.tcp_nodelay


def test_tcp_keepalive_raises_on_inproc_listener() -> None:
    """tcp_keepalive on an inproc listener raises NngNotSupported."""
    with nng.PairSocket() as srv:
        lst = srv.add_listener("inproc://test_tcp_opts_ka_listener")
        with pytest.raises(nng.NngNotSupported):
            _ = lst.tcp_keepalive


def test_pipe_tcp_nodelay_default_true_on_inproc() -> None:
    """Non-TCP pipes return True for tcp_nodelay (C++ default: _nodelay=true)."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tcp_opts_pipe_nd").start()
        cli.add_dialer("inproc://test_tcp_opts_pipe_nd").start()

        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.tcp_nodelay is True


def test_pipe_tcp_keepalive_default_false_on_inproc() -> None:
    """Non-TCP pipes return False for tcp_keepalive (C++ default: _keepalive=false)."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tcp_opts_pipe_ka").start()
        cli.add_dialer("inproc://test_tcp_opts_pipe_ka").start()

        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.tcp_keepalive is False

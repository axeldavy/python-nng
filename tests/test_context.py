"""Context unit tests.

Covers:
- Basic lifecycle: id, repr, close errors, socket property, context manager.
- Synchronous and asynchronous roundtrips.
- Async fallback path: socket Python ref released, connection kept alive by
  ContextHandle's shared_ptr<SocketHandle>.
- Connection lifetime: listener stays bound while explicit Python Listener ref
  and context both exist; released when all refs are dropped.
- Option inheritance: recv_timeout, send_timeout copied from socket at
  context-open time and independent afterwards.
- ReqContext.resend_time inheritance and independence.
- SurveyorContext.survey_time inheritance and independence.
- SubContext.recv_buf inheritance, independence, subscribe/unsubscribe.
"""
import asyncio
import gc
import time

import pytest

import nng

# Base URL prefix for all inproc endpoints used in this module.
_B = "inproc://test_ctx"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _req_rep(suffix: str, *, timeout_ms: int = 2000) -> tuple[nng.ReqSocket, nng.RepSocket]:
    """Return a connected (req, rep) pair on a unique inproc URL.

    The listener is started before the dialer so the inproc endpoint exists.
    """
    url = f"{_B}_{suffix}"
    rep = nng.RepSocket()
    rep.recv_timeout = timeout_ms
    rep.add_listener(url).start()
    req = nng.ReqSocket()
    req.recv_timeout = timeout_ms
    req.add_dialer(url).start()
    return req, rep


# ── Basic lifecycle ───────────────────────────────────────────────────────────

def test_context_id_is_positive() -> None:
    """open_context allocates a unique positive context ID."""
    with nng.ReqSocket() as req:
        c1 = req.open_context()
        c2 = req.open_context()
        assert c1.id > 0
        assert c2.id > 0
        assert c1.id != c2.id
        c1.close()
        c2.close()


def test_context_repr_open_and_closed() -> None:
    """repr contains the numeric id and reflects open/closed state."""
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        r = repr(ctx)
        assert "open=True" in r
        assert str(ctx.id) in r
        ctx.close()
        assert "open=False" in repr(ctx)


def test_context_used_as_context_manager() -> None:
    """Context closes on __exit__ and raises NngClosed on subsequent access."""
    with nng.ReqSocket() as req:
        with req.open_context() as ctx:
            assert ctx.id > 0
        with pytest.raises(nng.NngClosed):
            ctx.recv_timeout  # _check() inside the getter must raise


def test_context_socket_property_returns_parent() -> None:
    """context.socket returns the parent socket while both are alive."""
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        assert ctx.socket is req
        ctx.close()


# ── Errors on closed context ──────────────────────────────────────────────────

def test_closed_context_raises_on_send() -> None:
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        ctx.close()
        with pytest.raises(nng.NngClosed):
            ctx.send(b"x")


def test_closed_context_raises_on_recv() -> None:
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        ctx.close()
        with pytest.raises(nng.NngClosed):
            ctx.recv()


async def test_closed_context_raises_on_asend() -> None:
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        ctx.close()
        with pytest.raises(nng.NngClosed):
            await ctx.asend(b"x")


async def test_closed_context_raises_on_arecv() -> None:
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        ctx.close()
        with pytest.raises(nng.NngClosed):
            await ctx.arecv()


# ── Synchronous roundtrip ─────────────────────────────────────────────────────

def test_context_sync_roundtrip() -> None:
    """Context sync send → rep recv → rep send → context recv."""
    req, rep = _req_rep("sync")
    try:
        with req.open_context() as ctx:
            ctx.send(b"req-data")
            assert rep.recv() == b"req-data"
            rep.send(b"rep-data")
            assert ctx.recv() == b"rep-data"
    finally:
        req.close()
        rep.close()


# ── Async roundtrip (normal path, socket ref alive) ───────────────────────────

async def test_context_async_roundtrip() -> None:
    """Context async roundtrip with the socket Python reference still alive."""
    req, rep = _req_rep("async")
    try:
        async def _serve() -> None:
            msg = await rep.arecv()
            await rep.asend(msg)

        ctx = req.open_context()
        await asyncio.gather(_serve(), ctx.asend(b"hello"))
        assert await ctx.arecv() == b"hello"
        ctx.close()
    finally:
        req.close()
        rep.close()


# ── Async fallback (socket Python ref released) ───────────────────────────────

async def test_context_async_after_socket_ref_release() -> None:
    """asend/arecv work after the only Python reference to the socket is dropped.

    Motivation: ContextHandle holds a shared_ptr<SocketHandle>, so the nng
    socket cannot be destroyed as long as a live context exists.  Once the
    Python Socket wrapper is GC'd, _weak_socket_ref() returns None and
    asend/arecv fall back to _AioCbASync (no AioAsyncManager required).
    """
    req, rep = _req_rep("fallback")
    ctx = req.open_context()

    # Drop the only Python reference to the req socket.
    del req
    gc.collect()

    async def _serve() -> None:
        msg = await rep.arecv()
        await rep.asend(msg)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(_serve())
        tg.create_task(ctx.asend(b"ping"))
    assert await ctx.arecv() == b"ping"

    ctx.close()
    rep.close()


# ── Connection lifetime ───────────────────────────────────────────────────────

def test_socket_handle_alive_while_context_exists() -> None:
    """The nng socket stays alive long enough for a sync roundtrip after socket
    Python ref is GC'd.

    After ``del req`` the Python Socket wrapper is freed, but ContextHandle
    still holds a shared_ptr<SocketHandle> so the nng_socket is open.  The
    existing pipe is unaffected; synchronous send/recv remain functional.
    """
    req, rep = _req_rep("sh_alive_sync")
    ctx = req.open_context()

    del req
    gc.collect()

    # Synchronous roundtrip via the existing pipe: proves the socket is alive.
    ctx.send(b"still-alive")
    assert rep.recv() == b"still-alive"
    rep.send(b"confirmed")
    assert ctx.recv() == b"confirmed"

    ctx.close()
    rep.close()


def test_listener_released_when_all_python_refs_dropped() -> None:
    """The inproc endpoint is freed when every Python reference is released.

    Holding both an explicit Listener reference and a Context reference keeps
    the SocketHandle alive and the listener bound — a second socket cannot
    listen on the same URL.  Once both are released (triggering
    nng_listener_close and nng_socket_close), the URL is available again.
    """
    url = f"{_B}_released"

    rep = nng.RepSocket()
    # Hold an explicit reference to the Listener so that GCing `rep` alone
    # does not immediately close it (ListenerHandle::~ListenerHandle calls
    # nng_listener_close, so the Python wrapper must stay alive).
    lst = rep.add_listener(url)
    lst.start()

    req = nng.ReqSocket()
    req.add_dialer(url).start()

    # Context on the listener side keeps the SocketHandle alive.
    ctx = rep.open_context()

    del rep
    gc.collect()

    # lst (ListenerHandle) + ctx (ContextHandle) both hold the SocketHandle;
    # the listener is still bound and a new one cannot start.
    rep2 = nng.RepSocket()
    try:
        with pytest.raises(nng.NngAddressInUse):
            rep2.add_listener(url).start()
    finally:
        rep2.close()

    # Release all remaining references.
    ctx.close()
    del ctx
    del lst
    req.close()
    gc.collect()

    # Now the socket is fully closed and the inproc URL is free.
    rep3 = nng.RepSocket()
    try:
        rep3.add_listener(url).start()  # must not raise NngAddressInUse
    finally:
        rep3.close()


# ── Timeout inheritance ───────────────────────────────────────────────────────

def test_context_inherits_recv_timeout() -> None:
    """recv_timeout is copied from the socket at context-open time."""
    with nng.ReqSocket() as req:
        req.recv_timeout = 750
        ctx = req.open_context()
        assert ctx.recv_timeout == 750
        ctx.close()


def test_context_recv_timeout_independent_of_socket() -> None:
    """Changing socket recv_timeout after context creation has no effect on the context."""
    with nng.ReqSocket() as req:
        req.recv_timeout = 750
        ctx = req.open_context()
        req.recv_timeout = 1500
        assert ctx.recv_timeout == 750  # unchanged since context-open
        ctx.close()


def test_context_inherits_send_timeout() -> None:
    """send_timeout is copied from the socket at context-open time."""
    with nng.ReqSocket() as req:
        req.send_timeout = 600
        ctx = req.open_context()
        assert ctx.send_timeout == 600
        ctx.close()


def test_context_send_timeout_independent_of_socket() -> None:
    """Changing socket send_timeout after context creation has no effect on the context."""
    with nng.ReqSocket() as req:
        req.send_timeout = 600
        ctx = req.open_context()
        req.send_timeout = 1200
        assert ctx.send_timeout == 600  # unchanged since context-open
        ctx.close()


def test_context_timeouts_settable_directly() -> None:
    """recv_timeout and send_timeout can be overridden on the context directly."""
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        ctx.recv_timeout = 300
        ctx.send_timeout = 400
        assert ctx.recv_timeout == 300
        assert ctx.send_timeout == 400
        ctx.close()


# ── ReqContext.resend_time ────────────────────────────────────────────────────

def test_req_context_inherits_resend_time() -> None:
    """ReqContext.resend_time is copied from the socket at open time."""
    with nng.ReqSocket() as req:
        req.resend_time = 2000
        ctx = req.open_context()
        assert ctx.resend_time == 2000
        ctx.close()


def test_req_context_resend_time_independent_of_socket() -> None:
    """Changing socket resend_time after context creation has no effect on the context."""
    with nng.ReqSocket() as req:
        req.resend_time = 2000
        ctx = req.open_context()
        req.resend_time = 5000
        assert ctx.resend_time == 2000  # unchanged since context-open
        ctx.close()


def test_req_context_resend_time_settable() -> None:
    """resend_time can be overridden at the context level."""
    with nng.ReqSocket() as req:
        ctx = req.open_context()
        ctx.resend_time = 3000
        assert ctx.resend_time == 3000
        ctx.close()


# ── SurveyorContext.survey_time ───────────────────────────────────────────────

def test_surveyor_context_inherits_survey_time() -> None:
    """SurveyorContext.survey_time is copied from the socket at open time."""
    with nng.SurveyorSocket() as surv:
        surv.survey_time = 800
        ctx = surv.open_context()
        assert ctx.survey_time == 800
        ctx.close()


def test_surveyor_context_survey_time_independent_of_socket() -> None:
    """Changing socket survey_time after context creation does not affect the context."""
    with nng.SurveyorSocket() as surv:
        surv.survey_time = 800
        ctx = surv.open_context()
        surv.survey_time = 200
        assert ctx.survey_time == 800  # unchanged since context-open
        ctx.close()


def test_surveyor_context_survey_time_settable() -> None:
    """survey_time can be overridden at the context level."""
    with nng.SurveyorSocket() as surv:
        ctx = surv.open_context()
        ctx.survey_time = 1500
        assert ctx.survey_time == 1500
        ctx.close()


# ── SubContext.recv_buf ───────────────────────────────────────────────────────

def test_sub_context_inherits_recv_buf() -> None:
    """SubContext.recv_buf is copied from the socket's recv_buf at open time."""
    with nng.SubSocket() as sub:
        sub.recv_buf = 64
        ctx = sub.open_context()
        assert ctx.recv_buf == 64
        ctx.close()


def test_sub_context_recv_buf_independent_of_socket() -> None:
    """Changing socket recv_buf after context creation does not affect the context."""
    with nng.SubSocket() as sub:
        sub.recv_buf = 64
        ctx = sub.open_context()
        sub.recv_buf = 256
        assert ctx.recv_buf == 64  # unchanged since context-open
        ctx.close()


def test_sub_context_recv_buf_settable() -> None:
    """recv_buf can be set directly on a SubContext."""
    with nng.SubSocket() as sub:
        ctx = sub.open_context()
        ctx.recv_buf = 32
        assert ctx.recv_buf == 32
        ctx.close()


# ── SubContext subscribe / unsubscribe ────────────────────────────────────────

def test_sub_context_subscribe_receives_matching() -> None:
    """SubContext delivers messages that match its subscription prefix."""
    url = f"{_B}_subctx_sub"
    with nng.PubSocket() as pub, nng.SubSocket() as sub:
        pub.add_listener(url).start()
        sub.add_dialer(url).start()

        ctx = sub.open_context()
        ctx.recv_timeout = 2000
        ctx.subscribe(b"news.")

        time.sleep(0.02)  # allow the pipe to establish
        pub.send(b"news.sport")
        assert ctx.recv() == b"news.sport"
        ctx.close()


def test_sub_context_unsubscribe_stops_delivery() -> None:
    """After unsubscribing, matching messages no longer arrive on that context."""
    url = f"{_B}_subctx_unsub"
    with nng.PubSocket() as pub, nng.SubSocket() as sub:
        pub.add_listener(url).start()
        sub.add_dialer(url).start()

        ctx = sub.open_context()
        ctx.recv_timeout = 200
        ctx.subscribe(b"x.")

        time.sleep(0.02)
        pub.send(b"x.first")
        assert ctx.recv() == b"x.first"

        ctx.unsubscribe(b"x.")
        time.sleep(0.02)
        pub.send(b"x.second")
        with pytest.raises(nng.NngTimeout):
            ctx.recv()

        ctx.close()

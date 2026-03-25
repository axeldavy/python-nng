"""tests/test_threadsafety.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Thread-safety stress tests for the nng binding.

Goal
----
1.  Document thread-safety issues, using ``xfail`` when a behaviour is
    actively broken so regressions are tracked but do not block CI.  An XPASS
    means the issue was fixed and the marker should be removed.  Where issues
    have already been resolved the tests become plain regression guards.
2.  Provide regression guards for operations that the nng documentation
    describes as thread-safe.  If they break in a future update the tests turn
    red immediately.
3.  Cover realistic cross-thread usage patterns: fire-and-forget sends,
    producer/consumer pools, close-while-blocked, and asyncio contexts.

Resolved issues (previously xfail)
------------------------------------
``TestSubmitSendThenSyncOp``
    Calling ``recv()`` or ``submit_recv()`` on a REQ socket immediately after
    ``submit_send()`` – without first awaiting the send future – previously
    raised ``NngState`` (NNG_ESTATE).  The root cause was the Python binding
    silently swallowing nng errors when ``bytes`` were passed to ``send()``
    instead of a ``Message``, masking the true protocol state rather than
    propagating it.  After the ``Socket.send()`` / ``Context.send()``
    error-propagation fix all three patterns now pass unconditionally.
    The tests remain as regression guards; there are no ``xfail`` markers.

Naming conventions
------------------
* URL suffix: unique short string per test; full URL is
  ``inproc://ts_<suffix>`` so there is never a collision with other test
  modules.
* Test functions: ``test_<unit>_<scenario>``.
"""
import asyncio
import concurrent.futures
import threading
import time
from typing import Final

import pytest

import nng

_BASE: Final[str] = "inproc://ts"


def _url(suffix: str) -> str:
    """Return a unique inproc URL for *suffix*."""
    return f"{_BASE}_{suffix}"


def _rep_req(
    suffix: str,
    *,
    recv_timeout: int = 2000,
) -> tuple[nng.RepSocket, nng.ReqSocket]:
    """Return a connected ``(rep, req)`` pair.

    Caller is responsible for closing both sockets.
    """
    rep = nng.RepSocket()
    req = nng.ReqSocket()
    rep.recv_timeout = req.recv_timeout = recv_timeout
    rep.add_listener(_url(suffix)).start()
    req.add_dialer(_url(suffix)).start()
    return rep, req


# =============================================================================
# 1.  Regression guard: submit_send() → recv()/submit_recv() without waiting
# =============================================================================

class TestSubmitSendThenSyncOp:
    """submit_send() followed immediately by recv()/submit_recv() without waiting.

    Per the nng documentation all I/O operations are safe to call concurrently.
    A REQ ``submit_send()`` submits to nng's thread pool and returns; the
    caller should be free to issue ``recv()`` or ``submit_recv()`` immediately
    – nng should serialise them internally, queueing the recv until the state
    machine has advanced to RECV_ONLY.

    All three patterns now pass reliably after the Socket/Context send()
    error-propagation fix.  These tests serve as regression guards: if a
    future nng update reintroduces the race they will fail.
    """

    def test_req_submit_send_then_recv_no_wait(self) -> None:
        """submit_send() immediately followed by blocking recv() on same thread.

        Previously xfail: the REQ state machine returned NNG_ESTATE when
        recv() landed while the async send AIO had not yet transitioned from
        SENDING to RECV_ONLY.  The fix to Socket.send() error propagation
        resolved the underlying issue and this pattern now works reliably.
        """
        rep, req = _rep_req("ssrn")
        try:
            # Background echo worker
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            # Submit send but do NOT await the future before calling recv()
            send_fut = req.submit_send(b"ping")
            reply = req.recv()  # may hit NNG_ESTATE while still in SENDING state

            send_fut.result(timeout=2.0)
            assert reply.to_bytes() == b"ping"
            t.join(timeout=2.0)
        finally:
            rep.close()
            req.close()

    def test_req_submit_send_then_submit_recv_no_wait(self) -> None:
        """submit_send() immediately followed by submit_recv() without .result().

        Previously xfail: same race as above, but using submit_recv() instead
        of sync recv().  Now reliably passes after the error-propagation fix.
        """
        rep, req = _rep_req("sssr")
        try:
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            send_fut = req.submit_send(b"hello")
            # Submit recv immediately – racing against the send AIO
            recv_fut = req.submit_recv()

            send_fut.result(timeout=2.0)
            reply = recv_fut.result(timeout=2.0)
            assert reply.to_bytes() == b"hello"
            t.join(timeout=2.0)
        finally:
            rep.close()
            req.close()

    def test_req_context_submit_send_then_recv_no_wait(self) -> None:
        """Context.submit_send() immediately followed by Context.recv().

        Previously xfail: same NNG_ESTATE race on Context send.  Now
        reliably passes after the Context.send() error-propagation fix.
        """
        rep, req = _rep_req("cssr")
        ctx = req.open_context()
        try:
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            send_fut = ctx.submit_send(b"ctx-ping")
            reply = ctx.recv()  # may race with the send AIO

            send_fut.result(timeout=2.0)
            assert reply.to_bytes() == b"ctx-ping"
            t.join(timeout=2.0)
        finally:
            ctx.close()
            rep.close()
            req.close()


# =============================================================================
# 2.  Correct chaining: await send future before issuing recv (regression guard)
# =============================================================================

class TestSubmitChainWithWait:
    """submit_send().result() then submit_recv() works correctly.

    These are the patterns used in the simple_client.py example and should
    always pass.  They serve as a regression guard: if a nng update breaks
    them the tests turn red.
    """

    def test_req_submit_send_result_then_submit_recv(self) -> None:
        """submit_send().result() then submit_recv().result() – canonical pattern."""
        rep, req = _rep_req("scsr")
        try:
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            req.submit_send(b"world").result(timeout=2.0)
            reply = req.submit_recv().result(timeout=2.0)
            assert reply.to_bytes() == b"world"
            t.join(timeout=2.0)
        finally:
            rep.close()
            req.close()

    def test_req_sync_send_then_submit_recv(self) -> None:
        """Sync send() then submit_recv().result() – mixed API, same thread."""
        rep, req = _rep_req("sssr2")
        try:
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            req.send(b"abc")
            reply = req.submit_recv().result(timeout=2.0)
            assert reply.to_bytes() == b"abc"
            t.join(timeout=2.0)
        finally:
            rep.close()
            req.close()

    def test_req_submit_send_result_then_sync_recv(self) -> None:
        """submit_send().result() then sync recv() – mixed API, same thread."""
        rep, req = _rep_req("ssrsr")
        try:
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            req.submit_send(b"xyz").result(timeout=2.0)
            reply = req.recv()
            assert reply.to_bytes() == b"xyz"
            t.join(timeout=2.0)
        finally:
            rep.close()
            req.close()

    def test_req_context_full_round_trip(self) -> None:
        """Context.submit_send().result() then Context.submit_recv() – baseline."""
        rep, req = _rep_req("csfrt")
        ctx = req.open_context()
        try:
            def _echo() -> None:
                msg = rep.recv()
                rep.send(msg)

            t = threading.Thread(target=_echo, daemon=True)
            t.start()

            ctx.submit_send(b"ctx-ok").result(timeout=2.0)
            reply = ctx.submit_recv().result(timeout=2.0)
            assert reply.to_bytes() == b"ctx-ok"
            t.join(timeout=2.0)
        finally:
            ctx.close()
            rep.close()
            req.close()


# =============================================================================
# 3.  Concurrent REQ contexts from multiple threads (designed-for pattern)
# =============================================================================

class TestConcurrentContexts:
    """Many threads each own a Context on a shared socket.

    This is the intended concurrency model for REQ/REP: one socket per
    process, one Context per in-flight request.  All nng operations inside a
    Context are serialised by nng; the Python binding must not add extra
    locking that would break this.
    """

    def test_context_sync_concurrent_threads(self) -> None:
        """N threads × 1 context each, sync send/recv – all complete with correct replies."""
        N = 8
        rep, req = _rep_req("csct", recv_timeout=5000)
        errors: list[Exception] = []

        def _server() -> None:
            for _ in range(N):
                try:
                    msg = rep.recv()
                    rep.send(msg)
                except Exception as exc:
                    errors.append(exc)

        def _client(i: int) -> None:
            ctx = req.open_context()
            try:
                ctx.send(f"req-{i}".encode())
                reply = ctx.recv()
                assert reply.to_bytes() == f"req-{i}".encode()
            except Exception as exc:
                errors.append(exc)
            finally:
                ctx.close()

        try:
            srv = threading.Thread(target=_server, daemon=True)
            srv.start()
            clients = [threading.Thread(target=_client, args=(i,)) for i in range(N)]
            for c in clients:
                c.start()
            for c in clients:
                c.join(timeout=5.0)
            srv.join(timeout=5.0)
            assert not errors, f"Thread errors: {errors}"
        finally:
            rep.close()
            req.close()

    def test_context_submit_concurrent_threads(self) -> None:
        """N threads × 1 context each, submit_send/submit_recv – all complete correctly."""
        N = 8
        rep, req = _rep_req("csc2", recv_timeout=5000)
        errors: list[Exception] = []

        def _server() -> None:
            for _ in range(N):
                try:
                    msg = rep.recv()
                    rep.send(msg)
                except Exception as exc:
                    errors.append(exc)

        def _client(i: int) -> None:
            ctx = req.open_context()
            try:
                ctx.submit_send(f"item-{i}".encode()).result(timeout=3.0)
                reply = ctx.submit_recv().result(timeout=3.0)
                assert reply.to_bytes() == f"item-{i}".encode()
            except Exception as exc:
                errors.append(exc)
            finally:
                ctx.close()

        try:
            srv = threading.Thread(target=_server, daemon=True)
            srv.start()
            clients = [threading.Thread(target=_client, args=(i,)) for i in range(N)]
            for c in clients:
                c.start()
            for c in clients:
                c.join(timeout=5.0)
            srv.join(timeout=5.0)
            assert not errors, f"Thread errors: {errors}"
        finally:
            rep.close()
            req.close()

    def test_context_high_concurrency(self) -> None:
        """32 contexts in flight simultaneously – stress-tests the AIO dispatcher."""
        N = 32
        rep, req = _rep_req("cchc", recv_timeout=10_000)
        errors: list[Exception] = []

        def _server() -> None:
            for _ in range(N):
                try:
                    msg = rep.recv()
                    rep.send(msg)
                except Exception as exc:
                    errors.append(exc)

        def _client(i: int) -> None:
            ctx = req.open_context()
            try:
                ctx.submit_send(f"{i}".encode()).result(timeout=5.0)
                reply = ctx.submit_recv().result(timeout=5.0)
                assert reply.to_bytes() == f"{i}".encode()
            except Exception as exc:
                errors.append(exc)
            finally:
                ctx.close()

        try:
            srv = threading.Thread(target=_server, daemon=True)
            srv.start()
            clients = [threading.Thread(target=_client, args=(i,)) for i in range(N)]
            for c in clients:
                c.start()
            for c in clients:
                c.join(timeout=10.0)
            srv.join(timeout=10.0)
            assert not errors, f"Errors in {len(errors)} threads: {errors[:3]}"
        finally:
            rep.close()
            req.close()


# =============================================================================
# 4.  Concurrent PUSH / PULL from multiple producer / consumer threads
# =============================================================================

class TestConcurrentPushPull:
    """Thread-safe access on PUSH/PULL sockets – multiple producers / consumers."""

    def test_concurrent_push_from_threads(self) -> None:
        """N producer threads each call submit_send(); all N items are received."""
        N = 20
        url = _url("cpft")
        push = nng.PushSocket()
        pull = nng.PullSocket()
        push.send_timeout = pull.recv_timeout = 5000
        pull.add_listener(url).start()
        push.add_dialer(url).start()

        received: list[bytes] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _producer(i: int) -> None:
            try:
                push.submit_send(f"item-{i}".encode()).result(timeout=3.0)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        def _consumer() -> None:
            for _ in range(N):
                try:
                    msg = pull.submit_recv().result(timeout=3.0)
                    with lock:
                        received.append(msg.to_bytes())
                except Exception as exc:
                    with lock:
                        errors.append(exc)

        try:
            consumer_thread = threading.Thread(target=_consumer, daemon=True)
            consumer_thread.start()
            producers = [threading.Thread(target=_producer, args=(i,)) for i in range(N)]
            for p in producers:
                p.start()
            for p in producers:
                p.join(timeout=5.0)
            consumer_thread.join(timeout=5.0)
            assert not errors, f"Errors: {errors}"
            assert len(received) == N
        finally:
            push.close()
            pull.close()

    def test_concurrent_pull_from_threads(self) -> None:
        """N consumer threads each call submit_recv(); all N items are delivered."""
        N = 20
        url = _url("cplt")
        push = nng.PushSocket()
        pull = nng.PullSocket()
        push.send_timeout = pull.recv_timeout = 5000
        pull.add_listener(url).start()
        push.add_dialer(url).start()

        received: list[bytes] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _consumer() -> None:
            try:
                msg = pull.submit_recv().result(timeout=3.0)
                with lock:
                    received.append(msg.to_bytes())
            except Exception as exc:
                with lock:
                    errors.append(exc)

        try:
            # Start all consumers first so they are all blocking before any
            # message is sent.
            consumers = [threading.Thread(target=_consumer) for _ in range(N)]
            for c in consumers:
                c.start()
            time.sleep(0.05)

            for i in range(N):
                push.submit_send(f"work-{i}".encode()).result(timeout=3.0)
            for c in consumers:
                c.join(timeout=5.0)

            assert not errors, f"Errors: {errors}"
            assert len(received) == N
        finally:
            push.close()
            pull.close()

    def test_many_push_many_pull_threads(self) -> None:
        """Multiple producers and multiple consumers share the same push/pull socket pair."""
        N_PROD = 4
        N_MSG_PER_PROD = 10
        TOTAL = N_PROD * N_MSG_PER_PROD

        url_push = _url("mppl_push")
        url_pull = _url("mppl_pull")

        # Fan-out pattern: one push → one pull, multiple threads on each side
        push = nng.PushSocket()
        pull = nng.PullSocket()
        push.send_timeout = pull.recv_timeout = 5000
        pull.add_listener(url_push).start()
        push.add_dialer(url_push).start()

        received: list[bytes] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _producer(pid: int) -> None:
            for j in range(N_MSG_PER_PROD):
                try:
                    push.submit_send(f"p{pid}-m{j}".encode()).result(timeout=3.0)
                except Exception as exc:
                    with lock:
                        errors.append(exc)

        def _consumer() -> None:
            while True:
                # Check completion before submitting a new recv so we do not
                # spin with a pending future after all messages are collected.
                with lock:
                    if len(received) >= TOTAL:
                        return
                try:
                    msg = pull.submit_recv().result(timeout=1.0)
                    with lock:
                        received.append(msg.to_bytes())
                        if len(received) >= TOTAL:
                            return
                except nng.NngTimeout:
                    # nng-level timeout: no message arrived; loop and re-check.
                    pass
                except TimeoutError:
                    # concurrent.futures deadline on .result(): no message yet.
                    pass
                except nng.NngClosed:
                    return
                except Exception as exc:
                    with lock:
                        errors.append(exc)
                    return

        try:
            N_CONS = 4
            consumers = [threading.Thread(target=_consumer, daemon=True) for _ in range(N_CONS)]
            for c in consumers:
                c.start()

            producers = [threading.Thread(target=_producer, args=(i,)) for i in range(N_PROD)]
            for p in producers:
                p.start()
            for p in producers:
                p.join(timeout=10.0)
            for c in consumers:
                c.join(timeout=5.0)

            assert not errors, f"Errors: {errors}"
            assert len(received) == TOTAL, f"Expected {TOTAL}, got {len(received)}"
        finally:
            push.close()
            pull.close()


# =============================================================================
# 5.  Close-while-blocked race conditions
# =============================================================================

class TestCloseRace:
    """Closing a socket from one thread while another is blocked."""

    def test_close_unblocks_blocking_recv(self) -> None:
        """Calling close() from thread B unblocks thread A blocked in recv()."""
        url = _url("cubr")
        rep = nng.RepSocket()
        rep.add_listener(url).start()

        error: list[Exception | None] = [None]
        in_recv = threading.Event()

        def _blocked_recv() -> None:
            in_recv.set()
            try:
                rep.recv()
                error[0] = AssertionError("recv() returned without a message")
            except nng.NngClosed:
                pass  # expected: close unblocks with NngClosed
            except Exception as exc:
                error[0] = exc

        t = threading.Thread(target=_blocked_recv, daemon=True)
        t.start()
        in_recv.wait(timeout=1.0)
        time.sleep(0.02)  # small margin to ensure the thread is inside recv()
        rep.close()
        t.join(timeout=2.0)

        assert not t.is_alive(), "recv() thread did not exit after socket close"
        assert error[0] is None, f"Unexpected error: {error[0]}"

    def test_close_unblocks_submit_recv_future(self) -> None:
        """Closing the socket resolves a pending submit_recv() future with NngClosed."""
        url = _url("cusr")
        rep = nng.RepSocket()
        rep.add_listener(url).start()

        fut = rep.submit_recv()
        time.sleep(0.05)
        rep.close()

        with pytest.raises(nng.NngClosed):
            fut.result(timeout=2.0)

    def test_close_cancels_pending_submit_send(self) -> None:
        """Closing the socket resolves a pending submit_send() future with NngClosed.

        Only applies when the send cannot complete (no connected peer and send
        buffer exhausted).  Uses send_buf=0 so the very first send blocks.
        """
        url = _url("cusp")
        push = nng.PushSocket()
        push.send_buf = 0  # no buffering → send blocks immediately without a peer
        push.add_listener(url).start()

        fut = push.submit_send(b"x")
        time.sleep(0.05)
        push.close()

        with pytest.raises((nng.NngClosed, nng.NngError)):
            fut.result(timeout=2.0)

    def test_close_unblocks_blocking_send(self) -> None:
        """Closing the socket from thread B unblocks thread A blocked in send()."""
        url = _url("cubs")
        push = nng.PushSocket()
        push.send_buf = 0  # no buffering → send blocks without a peer
        push.add_listener(url).start()

        error: list[Exception | None] = [None]
        ready = threading.Event()

        def _blocked_send() -> None:
            ready.set()
            try:
                push.send(b"blocked")
                error[0] = AssertionError("send() returned without a recipient")
            except (nng.NngClosed, nng.NngError):
                pass  # expected: close unblocks send
            except Exception as exc:
                error[0] = exc

        t = threading.Thread(target=_blocked_send, daemon=True)
        t.start()
        ready.wait(timeout=1.0)
        time.sleep(0.02)
        push.close()
        t.join(timeout=2.0)

        assert not t.is_alive(), "send() thread did not exit after socket close"
        assert error[0] is None, f"Unexpected error: {error[0]}"

    def test_close_while_many_submit_recv_pending(self) -> None:
        """Close socket while N submit_recv() futures are all pending."""
        N = 10
        url = _url("cwmsr")
        pull = nng.PullSocket()
        pull.add_listener(url).start()

        futs = [pull.submit_recv() for _ in range(N)]
        time.sleep(0.05)
        pull.close()

        closed_count = 0
        for f in futs:
            try:
                f.result(timeout=2.0)
            except nng.NngClosed:
                closed_count += 1
            except Exception as exc:
                pytest.fail(f"Unexpected exception: {exc!r}")

        # All futures must be resolved (either NngClosed or, theoretically, a
        # message if one arrived between submit and close – though none did here).
        assert closed_count == N, f"Only {closed_count}/{N} futures resolved with NngClosed"


# =============================================================================
# 6.  Mixed sync/async API used from different threads
# =============================================================================

class TestMixedApiAcrossThreads:
    """submit_* and sync send/recv can be called from different threads."""

    def test_submit_recv_bg_thread_main_sends_sync(self) -> None:
        """Background thread calls submit_recv(); main thread does sync send()."""
        N = 10
        url = _url("srbt")
        push = nng.PushSocket()
        pull = nng.PullSocket()
        push.send_timeout = pull.recv_timeout = 5000
        pull.add_listener(url).start()
        push.add_dialer(url).start()

        futures: list[concurrent.futures.Future[nng.Message]] = [
            pull.submit_recv() for _ in range(N)
        ]

        try:
            for i in range(N):
                push.send(f"sync-{i}".encode())

            results = [f.result(timeout=3.0).to_bytes() for f in futures]
            assert len(results) == N
        finally:
            push.close()
            pull.close()

    def test_sync_recv_bg_thread_main_submits_send(self) -> None:
        """Background threads call sync recv(); main thread does submit_send()."""
        N = 10
        url = _url("srbt2")
        push = nng.PushSocket()
        pull = nng.PullSocket()
        push.send_timeout = pull.recv_timeout = 5000
        pull.add_listener(url).start()
        push.add_dialer(url).start()

        received: list[bytes] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _sync_recv() -> None:
            try:
                msg = pull.recv()
                with lock:
                    received.append(msg.to_bytes())
            except Exception as exc:
                with lock:
                    errors.append(exc)

        try:
            threads = [threading.Thread(target=_sync_recv) for _ in range(N)]
            for t in threads:
                t.start()
            time.sleep(0.05)  # let threads settle into recv()

            for i in range(N):
                push.submit_send(f"async-{i}".encode()).result(timeout=3.0)
            for t in threads:
                t.join(timeout=3.0)

            assert not errors, f"recv() errors: {errors}"
            assert len(received) == N
        finally:
            push.close()
            pull.close()


# =============================================================================
# 7.  Concurrent socket option read/write from multiple threads
# =============================================================================

class TestConcurrentOptionAccess:
    """Socket option getters/setters are thread-safe."""

    def test_concurrent_timeout_rw(self) -> None:
        """Multiple threads reading and writing recv_timeout simultaneously."""
        url = _url("cota")
        sock = nng.PullSocket()
        sock.add_listener(url).start()
        errors: list[Exception] = []

        def _rw_loop(i: int) -> None:
            for _ in range(100):
                try:
                    sock.recv_timeout = i * 10  # write
                    _ = sock.recv_timeout        # read
                except Exception as exc:
                    errors.append(exc)

        try:
            threads = [threading.Thread(target=_rw_loop, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
            assert not errors, f"Option access errors: {errors}"
        finally:
            sock.close()

    def test_concurrent_send_timeout_rw(self) -> None:
        """send_timeout can be read and written from multiple threads concurrently."""
        url = _url("cstr")
        sock = nng.PushSocket()
        sock.add_listener(url).start()
        errors: list[Exception] = []

        def _rw() -> None:
            for ms in range(0, 200, 10):
                try:
                    sock.send_timeout = ms
                    _ = sock.send_timeout
                except Exception as exc:
                    errors.append(exc)

        try:
            threads = [threading.Thread(target=_rw) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
            assert not errors, f"send_timeout errors: {errors}"
        finally:
            sock.close()


# =============================================================================
# 8.  Concurrent asyncio tasks with REQ contexts (regression guard)
# =============================================================================

class TestConcurrentAsyncContexts:
    """Multiple asyncio tasks, each owning a Context, run on the same socket."""

    @pytest.mark.asyncio
    async def test_async_req_contexts_concurrent(self) -> None:
        """N async tasks × 1 Context each – all receive correct replies."""
        N = 8
        url = _url("cac")
        rep = nng.RepSocket()
        req = nng.ReqSocket()
        rep.recv_timeout = req.recv_timeout = 5000
        rep.add_listener(url).start()
        req.add_dialer(url).start()

        async def _echo_server() -> None:
            for _ in range(N):
                msg = await rep.arecv()
                await rep.asend(msg)

        async def _req_task(i: int) -> bytes:
            ctx = req.open_context()
            try:
                await ctx.asend(f"task-{i}".encode())
                reply = await ctx.arecv()
                return reply.to_bytes()
            finally:
                ctx.close()

        try:
            client_results, *_ = await asyncio.gather(
                asyncio.gather(*[_req_task(i) for i in range(N)]),
                _echo_server(),
            )
            assert sorted(client_results) == sorted(
                f"task-{i}".encode() for i in range(N)
            )
        finally:
            rep.close()
            req.close()

    @pytest.mark.asyncio
    async def test_async_req_contexts_high_concurrency(self) -> None:
        """32 concurrent asyncio REQ contexts on one socket – stress test."""
        N = 32
        url = _url("cahc")
        rep = nng.RepSocket()
        req = nng.ReqSocket()
        rep.recv_timeout = req.recv_timeout = 10_000
        rep.add_listener(url).start()
        req.add_dialer(url).start()

        async def _echo_server() -> None:
            for _ in range(N):
                msg = await rep.arecv()
                await rep.asend(msg)

        async def _req_task(i: int) -> bytes:
            ctx = req.open_context()
            try:
                await ctx.asend(f"{i}".encode())
                reply = await ctx.arecv()
                return reply.to_bytes()
            finally:
                ctx.close()

        try:
            client_results, *_ = await asyncio.gather(
                asyncio.gather(*[_req_task(i) for i in range(N)]),
                _echo_server(),
            )
            assert sorted(client_results) == sorted(f"{i}".encode() for i in range(N))
        finally:
            rep.close()
            req.close()


# =============================================================================
# 9.  PUB / SUB concurrent subscribers (regression guard)
# =============================================================================

class TestConcurrentPubSub:
    """Multiple subscriber threads receive from a single publisher."""

    def test_pub_broadcast_to_concurrent_sub_threads(self) -> None:
        """Publisher broadcasts N messages; M subscriber threads each receive all N."""
        N_MSG = 10
        N_SUB = 4
        url = _url("cpbs")

        pub = nng.PubSocket()
        pub.send_timeout = 5000
        pub.add_listener(url).start()

        subs: list[nng.SubSocket] = []
        received: list[list[bytes]] = [[] for _ in range(N_SUB)]
        errors: list[Exception] = []
        lock = threading.Lock()

        for i in range(N_SUB):
            s = nng.SubSocket()
            s.recv_timeout = 5000
            s.add_dialer(url).start()
            s.subscribe(b"")  # receive everything
            subs.append(s)

        # Let connections stabilise
        time.sleep(0.1)

        def _subscriber(idx: int) -> None:
            for _ in range(N_MSG):
                try:
                    msg = subs[idx].recv()
                    with lock:
                        received[idx].append(msg.to_bytes())
                except Exception as exc:
                    with lock:
                        errors.append(exc)

        try:
            threads = [
                threading.Thread(target=_subscriber, args=(i,)) for i in range(N_SUB)
            ]
            for t in threads:
                t.start()
            time.sleep(0.02)  # ensure all are blocked in recv()

            for i in range(N_MSG):
                pub.send(f"msg-{i}".encode())
                time.sleep(0.005)  # small pacing to avoid overwhelming buffers

            for t in threads:
                t.join(timeout=5.0)

            assert not errors, f"Subscriber errors: {errors}"
            for i, msgs in enumerate(received):
                assert len(msgs) == N_MSG, (
                    f"Subscriber {i} received {len(msgs)}/{N_MSG} messages"
                )
        finally:
            pub.close()
            for s in subs:
                s.close()

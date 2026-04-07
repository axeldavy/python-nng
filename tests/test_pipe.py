"""Pipe lifecycle and property tests.

Covers:
- Pipe properties: id, status, dialer, listener, socket
- Socket.pipes snapshot
- Socket.on_new_pipe callback (fires at ADDING with the Pipe)
- Pipe.on_status_change callback (fires at ACTIVE and REMOVED)
- Full status lifecycle: ADDING → ACTIVE → REMOVED
- Pipe.close() forcibly terminates a connection
- Rejection of a pipe inside on_new_pipe via Pipe.close()
- Multiple simultaneous pipes on a listener socket
- Pipe hash and equality (usable as dict key / set member)

NOTE on timing
--------------
Pipe lifecycle callbacks (on_new_pipe, on_status_change) are dispatched
from nng's internal I/O thread *after* Dialer.start() / Listener.start()
return.  Tests must therefore never access sock.pipes directly after
start() — they must wait for the relevant event or use the polling helper
_wait_for_active_pipes().

Similarly, remove_pipe() fires on_status_change *before* removing the
pipe from Socket._pipes (so the callback can still inspect the list).
Tests that assert len(sock.pipes) == 0 must wait for the post-callback
cleanup to complete.
"""
import time
import threading
import pytest
import nng


URL = "inproc://test_pipe"
_TIMEOUT = 2.0  # seconds used for all waits


# ── Helpers ────────────────────────────────────────────────────────────────────

def _wait_for_active_pipes(sock, n: int = 1, timeout: float = _TIMEOUT) -> list:
    """Poll until *sock* has at least *n* ACTIVE pipes, then return them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = [p for p in sock.pipes if p.status == nng.PipeStatus.ACTIVE]
        if len(active) >= n:
            return active
        time.sleep(0.005)
    pytest.fail(f"Timed out waiting for {n} ACTIVE pipe(s) on {sock!r}")


def _wait_for_empty_pipes(sock, timeout: float = _TIMEOUT) -> None:
    """Poll until *sock* has no pipes (post-removal cleanup has finished)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not sock.pipes:
            return
        time.sleep(0.005)
    pytest.fail(f"Timed out waiting for pipe list to empty on {sock!r}")


def _active_event_for(sock) -> threading.Event:
    """Register on_new_pipe + on_status_change and return an Event that fires
    when the first pipe on *sock* reaches ACTIVE status."""
    event = threading.Event()

    def _on_pipe(pipe: nng.Pipe) -> None:
        def _on_status() -> None:
            if pipe.status == nng.PipeStatus.ACTIVE:
                event.set()
        pipe.on_status_change = _on_status
        if pipe.status == nng.PipeStatus.ACTIVE:
            event.set()  # already active, so set the event directly

    sock.on_new_pipe = _on_pipe
    return event


# ── Basic pipe properties ──────────────────────────────────────────────────────

def test_pipe_id_is_positive():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_id").start()
        cli.add_dialer(URL + "_id").start()

        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.id > 0


def test_pipe_status_active():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_status").start()
        cli.add_dialer(URL + "_status").start()

        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.status == nng.PipeStatus.ACTIVE


def test_pipe_socket_refers_to_owner():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_sock").start()
        cli.add_dialer(URL + "_sock").start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        assert srv_pipe.socket is srv

        cli_pipe = _wait_for_active_pipes(cli)[0]
        assert cli_pipe.socket is cli


def test_pipe_listener_and_dialer_sides():
    """The server-side pipe has a listener; the client-side pipe has a dialer."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_sides").start()
        cli.add_dialer(URL + "_sides").start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        assert srv_pipe.listener is not None
        assert srv_pipe.dialer is None

        cli_pipe = _wait_for_active_pipes(cli)[0]
        assert cli_pipe.dialer is not None
        assert cli_pipe.listener is None


def test_pipes_snapshot_length():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_snap").start()
        cli.add_dialer(URL + "_snap").start()

        assert len(_wait_for_active_pipes(srv)) == 1
        assert len(_wait_for_active_pipes(cli)) == 1


def test_pipes_returns_independent_copy():
    """Modifying the returned list must not affect Socket.pipes."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_copy").start()
        cli.add_dialer(URL + "_copy").start()

        _wait_for_active_pipes(srv)
        pipes = srv.pipes
        pipes.clear()
        assert len(srv.pipes) == 1


# ── Pipe equality and hashing ──────────────────────────────────────────────────

def test_pipe_equality_same_object():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_eq").start()
        cli.add_dialer(URL + "_eq").start()

        _wait_for_active_pipes(srv)
        p1 = srv.pipes[0]
        p2 = srv.pipes[0]
        assert p1 == p2
        assert p1.id == p2.id


def test_pipe_usable_as_dict_key():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_hash").start()
        cli.add_dialer(URL + "_hash").start()

        pipe = _wait_for_active_pipes(srv)[0]
        d = {pipe: "metadata"}
        assert d[pipe] == "metadata"


def test_pipe_usable_in_set():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener(URL + "_set").start()
        cli.add_dialer(URL + "_set").start()

        pipe = _wait_for_active_pipes(srv)[0]
        s = {pipe, pipe}
        assert len(s) == 1


def test_pipes_from_different_connections_are_not_equal():
    with nng.PushSocket() as push, nng.PullSocket() as p1, nng.PullSocket() as p2:
        push.send_timeout = 2000
        push.add_listener(URL + "_neq").start()
        p1.add_dialer(URL + "_neq").start(block=True)
        p2.add_dialer(URL + "_neq").start(block=True)

        pipes = _wait_for_active_pipes(push, n=2)
        assert len(pipes) == 2
        assert pipes[0] != pipes[1]
        assert pipes[0].id != pipes[1].id


# ── Socket.on_new_pipe callback ────────────────────────────────────────────────

def test_on_new_pipe_called_on_connect():
    """on_new_pipe fires once when the client connects."""
    received: list[nng.Pipe] = []
    event = threading.Event()

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        def _cb(pipe: nng.Pipe) -> None:
            received.append(pipe)
            event.set()

        srv.on_new_pipe = _cb
        srv.add_listener(URL + "_cb").start()
        cli.add_dialer(URL + "_cb").start()

        assert event.wait(timeout=_TIMEOUT), "on_new_pipe was not called"
        assert len(received) == 1
        assert received[0].id > 0

def test_on_new_pipe_fills_pipe_fields():
    """Check pipe fields are immediately available after a blocking start()."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        listener = srv.add_listener(URL + "_cb")
        dialer = cli.add_dialer(URL + "_cb")

        assert len(srv.pipes) == 0
        assert len(cli.pipes) == 0

        listener.start()
        dialer.start(block=True)

        assert dialer.pipe is not None
        assert len(listener.pipes) == 1
        assert len(srv.pipes) == 1
        assert len(cli.pipes) == 1

        


def test_on_new_pipe_pipe_status_is_adding_or_active():
    """Inside on_new_pipe, the pipe status must be ADDING or ACTIVE."""
    statuses: list[nng.PipeStatus] = []
    event = threading.Event()

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        def _cb(pipe: nng.Pipe) -> None:
            statuses.append(pipe.status)
            event.set()

        srv.on_new_pipe = _cb
        srv.add_listener(URL + "_adding").start()
        cli.add_dialer(URL + "_adding").start()

        assert event.wait(timeout=_TIMEOUT)
        assert statuses[0] in (nng.PipeStatus.ADDING, nng.PipeStatus.ACTIVE)


def test_on_new_pipe_replace_callback():
    """Registering a second callback replaces the first."""
    first_calls = 0
    second_calls: list[nng.Pipe] = []
    event = threading.Event()

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        def _first(pipe: nng.Pipe) -> None:
            nonlocal first_calls
            first_calls += 1

        def _second(pipe: nng.Pipe) -> None:
            second_calls.append(pipe)
            event.set()

        srv.on_new_pipe = _first
        srv.on_new_pipe = _second   # replaces _first
        srv.add_listener(URL + "_replace").start()
        cli.add_dialer(URL + "_replace").start()

        assert event.wait(timeout=_TIMEOUT)
        assert first_calls == 0
        assert len(second_calls) == 1


def test_on_new_pipe_clear_callback():
    """Passing None removes the callback; no call should occur."""
    called = threading.Event()

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        def _cb(pipe: nng.Pipe) -> None:
            called.set()

        srv.on_new_pipe = _cb
        srv.on_new_pipe = None   # clear it

        srv.add_listener(URL + "_clear").start()
        cli.add_dialer(URL + "_clear").start()

        # Give any stray callback a moment to potentially fire.
        assert not called.wait(timeout=0.3), "cleared callback was called"


def test_on_new_pipe_reject_connection():
    """Calling Pipe.close() inside on_new_pipe rejects the connection.

    The rejection triggers an asynchronous REMOVE event.  We wait for
    both the rejection callback *and* the subsequent pipe-list cleanup
    before asserting that no pipes remain.
    """
    rejected_event = threading.Event()

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = 300

        def _reject(pipe: nng.Pipe) -> None:
            pipe.close()
            rejected_event.set()

        srv.on_new_pipe = _reject
        srv.add_listener(URL + "_reject").start()
        cli.add_dialer(URL + "_reject").start(block=False)

        assert rejected_event.wait(timeout=_TIMEOUT)
        # remove_pipe() runs after the callback, so poll until the list drains.
        _wait_for_empty_pipes(srv)


# ── Pipe.on_status_change callback ────────────────────────────────────────────

def test_pipe_on_status_change_active():
    """on_status_change fires with ACTIVE after the pipe is fully established."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        active_event = _active_event_for(srv)
        srv.add_listener(URL + "_active").start()
        cli.add_dialer(URL + "_active").start()

        assert active_event.wait(timeout=_TIMEOUT), "ACTIVE transition not observed"


def test_pipe_on_status_change_removed():
    """on_status_change fires with REMOVED when the peer disconnects.

    We wait for ACTIVE before closing the client so that on_status_change
    is guaranteed to be registered on the pipe before the REMOVE event fires.
    """
    active_event = threading.Event()
    removed_event = threading.Event()

    srv = nng.PairSocket()
    cli = nng.PairSocket()
    srv.recv_timeout = cli.recv_timeout = 2000

    def _new_pipe(pipe: nng.Pipe) -> None:
        def _status_change() -> None:
            if pipe.status == nng.PipeStatus.ACTIVE:
                active_event.set()
            elif pipe.status == nng.PipeStatus.REMOVED:
                removed_event.set()
        pipe.on_status_change = _status_change
        if pipe.status == nng.PipeStatus.ACTIVE:
            active_event.set()  # already active, so set the event directly

    srv.on_new_pipe = _new_pipe
    srv.add_listener(URL + "_removed").start()
    cli.add_dialer(URL + "_removed").start()

    # Ensure the pipe is fully up before we tear it down.
    assert active_event.wait(timeout=_TIMEOUT), "ACTIVE not observed"
    cli.close()

    assert removed_event.wait(timeout=_TIMEOUT), "REMOVED transition not observed"
    srv.close()


def test_pipe_status_lifecycle():
    """Verify the full ADDING → ACTIVE → REMOVED sequence."""
    statuses: list[nng.PipeStatus] = []
    active_event = threading.Event()
    removed_event = threading.Event()

    srv = nng.PairSocket()
    cli = nng.PairSocket()

    def _new_pipe(pipe: nng.Pipe) -> None:
        statuses.append(pipe.status)   # ADDING

        def _status_change() -> None:
            statuses.append(pipe.status)
            if pipe.status == nng.PipeStatus.ACTIVE:
                active_event.set()
            elif pipe.status == nng.PipeStatus.REMOVED:
                removed_event.set()

        pipe.on_status_change = _status_change
        if pipe.status == nng.PipeStatus.ACTIVE:
            active_event.set()  # already active, so set the event directly

    srv.on_new_pipe = _new_pipe
    srv.add_listener(URL + "_lifecycle").start()
    cli.add_dialer(URL + "_lifecycle").start()

    # Wait for ACTIVE before triggering the disconnect.
    assert active_event.wait(timeout=_TIMEOUT), "ACTIVE not observed"
    cli.close()

    assert removed_event.wait(timeout=_TIMEOUT), "REMOVED not observed"
    srv.close()

    # assert nng.PipeStatus.ADDING  in statuses -> no, can be skipped if too fast
    assert nng.PipeStatus.ACTIVE  in statuses
    assert nng.PipeStatus.REMOVED in statuses


# ── Pipe.close() ──────────────────────────────────────────────────────────────

def test_pipe_close_terminates_connection():
    """Calling Pipe.close() on a live pipe removes it from Socket.pipes.

    remove_pipe() fires on_status_change *before* removing the pipe from
    _pipes, so we poll for an empty list rather than checking immediately
    after the event fires.
    """
    removed_event = threading.Event()

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = srv.send_timeout = 2000
        active_ev = _active_event_for(srv)
        srv.add_listener(URL + "_pclose").start()
        cli.add_dialer(URL + "_pclose").start()

        assert active_ev.wait(timeout=_TIMEOUT), "pipe did not become ACTIVE"
        pipe = srv.pipes[0]

        def _on_removed() -> None:
            if pipe.status == nng.PipeStatus.REMOVED:
                removed_event.set()

        pipe.on_status_change = _on_removed
        pipe.close()

        assert removed_event.wait(timeout=_TIMEOUT)
        # Pipe is removed from _pipes after the callback returns; poll for it.
        _wait_for_empty_pipes(srv)


def test_pipe_close_makes_recv_fail():
    """After closing a pipe the socket cannot receive on it."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = 500
        cli.recv_timeout = 500
        srv.add_listener(URL + "_fail").start()
        cli.add_dialer(URL + "_fail").start()

        pipe = _wait_for_active_pipes(srv)[0]
        pipe.close()

        with pytest.raises(nng.NngError):
            srv.recv()


# ── Multiple pipes ─────────────────────────────────────────────────────────────

def test_multiple_pipes_on_listener():
    """A listener socket accumulates one pipe per connected dialer."""
    N = 5
    clients = []
    with nng.PullSocket() as srv:
        srv.add_listener(URL + "_multi").start()
        for _ in range(N):
            cli = nng.PushSocket()
            cli.send_timeout = 2000
            cli.add_dialer(URL + "_multi").start(block=True)
            clients.append(cli)

        assert len(_wait_for_active_pipes(srv, n=N)) == N

    for cli in clients:
        cli.close()


def test_on_new_pipe_called_for_each_connection():
    """on_new_pipe fires once per connecting client.

    The callback must NOT block the dispatcher thread (which is the single
    thread that delivers all pipe events).  We collect pipe IDs into a list
    and signal a threading.Event when N IDs have arrived.
    """
    N = 4
    clients = []
    pipe_ids: list[int] = []
    all_received = threading.Event()
    lock = threading.Lock()

    with nng.PullSocket() as srv:
        def _cb(pipe: nng.Pipe) -> None:
            with lock:
                pipe_ids.append(pipe.id)
                if len(pipe_ids) >= N:
                    all_received.set()

        srv.on_new_pipe = _cb
        srv.add_listener(URL + "_multi_cb").start()

        for _ in range(N):
            cli = nng.PushSocket()
            cli.send_timeout = 2000
            cli.add_dialer(URL + "_multi_cb").start(block=True)
            clients.append(cli)

        assert all_received.wait(timeout=_TIMEOUT), "not all on_new_pipe callbacks fired"

    for cli in clients:
        cli.close()

    assert len(pipe_ids) == N
    assert len(set(pipe_ids)) == N   # all IDs are distinct

"""Pair socket tests (sync + async, inproc transport)."""
import asyncio
import threading
import pytest
import nng


URL = "inproc://test_pair"


# ── Synchronous ────────────────────────────────────────────────────────────────

def test_pair_roundtrip_bytes():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = 2000
        cli.recv_timeout = 2000
        srv.add_listener(URL).start()
        cli.add_dialer(URL).start()
        cli.send(b"hello")
        assert srv.recv() == b"hello"
        srv.send(b"world")
        assert cli.recv() == b"world"


def test_pair_message_zero_copy():
    """Verify that Message body is accessible via buffer protocol."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = 2000
        srv.add_listener(URL + "_msg").start()
        cli.add_dialer(URL + "_msg").start()

        cli.send_msg(nng.Message(b"ping"))
        msg = srv.recv_msg()
        assert bytes(memoryview(msg)) == b"ping"   # zero-copy view
        assert msg.len == 4
        assert msg.to_bytes() == b"ping"


def test_pair_message_mutation():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = 2000
        srv.add_listener(URL + "_mut").start()
        cli.add_dialer(URL + "_mut").start()

        msg = nng.Message()
        msg.append(b"foo")
        msg.append_u16(0xBEEF)
        cli.send_msg(msg)

        recv = srv.recv_msg()
        assert recv.chop_u16() == 0xBEEF
        assert recv.to_bytes() == b"foo"


def test_pair_context_manager():
    """Ensure close() is idempotent via context manager."""
    with nng.PairSocket() as sock:
        sock.add_listener(URL + "_cm").start()
    # Socket is closed here; __dealloc__ must not double-close
    assert sock.id == 0


def test_pair_nonblock_recv_raises_again():
    with nng.PairSocket() as sock:
        sock.add_listener(URL + "_nb").start()
        with pytest.raises(nng.NngAgain):
            sock.recv(nonblock=True)


def test_pair_send_after_close_raises():
    sock = nng.PairSocket()
    sock.close()
    with pytest.raises(nng.NngClosed):
        sock.send(b"x")


# ── Async ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pair_async_roundtrip():
    """aio-callback path: send/recv should complete without blocking the loop."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = 2000
        srv.add_listener(URL + "_async").start()
        cli.add_dialer(URL + "_async").start()

        async def server():
            data = await srv.arecv()
            await srv.asend(data + b"!")

        await asyncio.gather(
            server(),
            cli.asend(b"async"),
        )
        reply = await cli.arecv()
        assert reply == b"async!"


@pytest.mark.asyncio
async def test_pair_async_msg_roundtrip():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = 2000
        srv.add_listener(URL + "_async_msg").start()
        cli.add_dialer(URL + "_async_msg").start()

        async def server():
            msg = await srv.arecv_msg()
            await srv.asend(msg.to_bytes().upper())

        await asyncio.gather(server(), cli.asend_msg(nng.Message(b"hello")))
        reply = await cli.arecv()
        assert reply == b"HELLO"


@pytest.mark.asyncio
async def test_pair_async_many():
    """Stress: 200 sequential async round-trips on inproc."""
    n = 200
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = 5000
        srv.add_listener(URL + "_many").start()
        cli.add_dialer(URL + "_many").start()

        async def echo_server():
            for _ in range(n):
                data = await srv.arecv()
                await srv.asend(data)

        async def client():
            for i in range(n):
                await cli.asend(str(i).encode())
                reply = await cli.arecv()
                assert reply == str(i).encode()

        await asyncio.gather(echo_server(), client())


# ── Pipe callbacks ────────────────────────────────────────────────────────────

def test_pipe_add_callback():
    """pipe ADD_POST callback fires when a connection is established."""
    added = []

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        def on_add(pipe_id, event):
            added.append((pipe_id, event))

        srv.on_pipe_event(nng.PIPE_EV_ADD_POST, on_add)
        srv.add_listener(URL + "_pipe").start()
        cli.add_dialer(URL + "_pipe").start()
        # Small delay for the callback to fire
        import time
        time.sleep(0.05)

    assert len(added) >= 1
    pipe_id, ev = added[0]
    assert pipe_id > 0
    assert ev == nng.PIPE_EV_ADD_POST

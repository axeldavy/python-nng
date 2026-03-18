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

        cli.send(nng.Message(b"ping"))
        msg = srv.recv()
        assert bytes(memoryview(msg)) == b"ping"   # zero-copy view
        assert len(msg) == 4
        assert msg.to_bytes() == b"ping"


def test_pair_message_mutation():
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = 2000
        srv.add_listener(URL + "_mut").start()
        cli.add_dialer(URL + "_mut").start()

        msg = nng.Message()
        msg.append(b"foo")
        msg.append_u16(0xBEEF)
        cli.send(msg)

        recv = srv.recv()
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
            data.append(b"!")
            await srv.asend(data)

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
            msg = await srv.arecv()
            await srv.asend(msg.to_bytes().upper())

        await asyncio.gather(server(), cli.asend(nng.Message(b"hello")))
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
"""Request/Reply tests – sync and async, with Context for concurrency."""
import asyncio
import pytest
import nng


URL = "inproc://test_reqrep"


# ── Synchronous ────────────────────────────────────────────────────────────────

def test_reqrep_basic():
    with nng.RepSocket() as rep, nng.ReqSocket() as req:
        rep.recv_timeout = req.recv_timeout = 2000
        rep.add_listener(URL).start()
        req.add_dialer(URL).start()

        req.send(b"ping")
        assert rep.recv() == b"ping"
        rep.send(b"pong")
        assert req.recv() == b"pong"


def test_reqrep_message():
    with nng.RepSocket() as rep, nng.ReqSocket() as req:
        rep.recv_timeout = req.recv_timeout = 2000
        rep.add_listener(URL + "_msg").start()
        req.add_dialer(URL + "_msg").start()

        req.send(nng.Message(b"hello"))
        msg = rep.recv()
        assert msg.to_bytes() == b"hello"
        rep.send(nng.Message(b"world"))
        assert req.recv() == b"world"


def test_reqrep_resend_time():
    with nng.ReqSocket() as req:
        req.resend_time = 500
        assert req.resend_time == 500


# ── Async ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reqrep_async():
    with nng.RepSocket() as rep, nng.ReqSocket() as req:
        rep.recv_timeout = req.recv_timeout = 2000
        rep.add_listener(URL + "_async").start()
        req.add_dialer(URL + "_async").start()

        async def server():
            data = await rep.arecv()
            await rep.asend(data.to_bytes()[::-1])    # reverse

        await asyncio.gather(server(), req.asend(b"abcde"))
        reply = await req.arecv()
        assert reply == b"edcba"


# ── Context (concurrent req/rep) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_concurrent():
    """Multiple contexts on one socket can be in-flight simultaneously."""
    N = 10
    with nng.RepSocket() as rep, nng.ReqSocket() as req:
        rep.recv_timeout = req.recv_timeout = 5000
        rep.add_listener(URL + "_ctx").start()
        req.add_dialer(URL + "_ctx").start()

        async def echo_server():
            for _ in range(N):
                data = await rep.arecv()
                await rep.asend(data)

        async def send_all():
            ctxs = [req.open_context() for _ in range(N)]
            try:
                tasks = []
                for i, ctx in enumerate(ctxs):
                    tasks.append(_ctx_roundtrip(ctx, str(i).encode()))
                results = await asyncio.gather(*tasks)
                assert sorted(results) == [str(i).encode() for i in range(N)]
            finally:
                for ctx in ctxs:
                    ctx.close()

        await asyncio.gather(echo_server(), send_all())


async def _ctx_roundtrip(ctx: nng.Context, data: bytes) -> bytes:
    await ctx.asend(data)
    return (await ctx.arecv()).to_bytes()


def test_context_sync():
    with nng.RepSocket() as rep, nng.ReqSocket() as req:
        rep.recv_timeout = req.recv_timeout = 2000
        rep.add_listener(URL + "_ctx_sync").start()
        req.add_dialer(URL + "_ctx_sync").start()

        with req.open_context() as ctx:
            ctx.send(b"ctx-hello")
            assert rep.recv() == b"ctx-hello"
            rep.send(b"ctx-world")
            assert ctx.recv() == b"ctx-world"

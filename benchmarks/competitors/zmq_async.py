"""Benchmark competitor: ZeroMQ async REQ/REP via zmq.asyncio (pyzmq)."""

import asyncio
import threading
import time

import zmq
import zmq.asyncio as azmq

from .base import BaseBenchmark
from .._core.common import COMPETITORS, run_in_new_loop

# For zmq inproc, both endpoints must share the same zmq context.
# The runner uses threads for inproc (same process), so we share via a
# module-level dict keyed by URL.  For ipc/tcp the runner uses separate
# processes so each side creates its own context.
_ZMQ_ASYNC_INPROC_CTX: dict[str, azmq.Context] = {}
_ASYNC_CTX_LOCK = threading.Lock()


def _get_or_create_async_ctx(url: str) -> tuple[azmq.Context, bool]:
    """Return (ctx, own_ctx).  own_ctx=True means the caller should term() it."""
    if url.startswith("inproc://"):
        with _ASYNC_CTX_LOCK:
            if url not in _ZMQ_ASYNC_INPROC_CTX:
                _ZMQ_ASYNC_INPROC_CTX[url] = azmq.Context()
            return _ZMQ_ASYNC_INPROC_CTX[url], False
    return azmq.Context(), True


class ZmqAsyncBenchmark(BaseBenchmark):
    """Measures pyzmq async (zmq.asyncio)"""

    name = "zmq_async"

    # ------------------------------------------------------------------
    # Server entry point
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        ctx, own_ctx = _get_or_create_async_ctx(url)

        async def _serve() -> None:
            sock = ctx.socket(zmq.REP)
            sock.bind(url)
            ready.set()
            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(sock.recv(), timeout=0.2)
                    await sock.send(b"")
                except asyncio.TimeoutError:
                    pass
            sock.close(linger=0)

        run_in_new_loop(_serve())
        if own_ctx:
            ctx.term()
        with _ASYNC_CTX_LOCK:
            _ZMQ_ASYNC_INPROC_CTX.pop(url, None)

    # ------------------------------------------------------------------
    # Latency
    # ------------------------------------------------------------------

    def measure_latency(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        ctx, own_ctx = _get_or_create_async_ctx(transport_url)
        payload = bytes(msg_size)

        async def _client() -> list[float]:
            sock = ctx.socket(zmq.REQ)
            sock.connect(transport_url)
            samples: list[float] = []
            for _ in range(n_warmup):
                await sock.send(payload)
                await sock.recv()
            for _ in range(n_iters):
                t0 = time.perf_counter()
                await sock.send(payload)
                await sock.recv()
                t1 = time.perf_counter()
                samples.append((t1 - t0) * 1e6)
            sock.close(linger=0)
            return samples

        result = run_in_new_loop(_client())
        if own_ctx:
            ctx.term()
        return result

    # ------------------------------------------------------------------
    # Ops/sec
    # ------------------------------------------------------------------

    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        ctx, own_ctx = _get_or_create_async_ctx(transport_url)
        payload = bytes(msg_size)

        async def _client() -> float:
            sock = ctx.socket(zmq.REQ)
            sock.connect(transport_url)
            count = 0
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                await sock.send(payload)
                await sock.recv()
                count += 1
            sock.close(linger=0)
            return count / duration_s

        result = run_in_new_loop(_client())
        if own_ctx:
            ctx.term()
        return result


COMPETITORS["zmq_async"] = ZmqAsyncBenchmark

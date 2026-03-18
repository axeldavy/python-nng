"""Benchmark competitor: ZeroMQ synchronous REQ/REP (pyzmq)."""

from __future__ import annotations

import threading
import time
from weakref import WeakValueDictionary

import zmq

from .base import BaseBenchmark
from .._core.common import COMPETITORS

# ZMQ uses slightly different URL scheme:
# inproc needs a shared Context; ipc and tcp are identical to nng.
_ZMQ_INPROC_CTX: WeakValueDictionary[str, zmq.Context] = WeakValueDictionary()
_CTX_LOCK = threading.Lock()

def _get_ctx(url: str) -> zmq.Context:
    """Return a shared zmq.Context for inproc:// URLs (required by zmq)."""
    if url.startswith("inproc://"):
        with _CTX_LOCK:
            if url in _ZMQ_INPROC_CTX:
                ctx = _ZMQ_INPROC_CTX[url]
            else:
                ctx = zmq.Context()
                _ZMQ_INPROC_CTX[url] = ctx
            return ctx
    return zmq.Context()


class ZmqSyncBenchmark(BaseBenchmark):
    """Measures pyzmq with blocking send/recv."""

    name = "zmq_sync"

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    @staticmethod
    def _run_server(
        url: str,
        ctx: zmq.Context,
        ready,
        stop,
    ) -> None:
        sock = ctx.socket(zmq.REP)
        sock.RCVTIMEO = 200  # ms
        sock.bind(url)
        ready.set()
        while not stop.is_set():
            try:
                msg = sock.recv()
                sock.send(b"")
            except zmq.Again:
                pass
        sock.close(linger=0)

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        ctx = _get_ctx(url)
        cls._run_server(url, ctx, ready, stop)

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
        payload = bytes(msg_size)
        ctx = _get_ctx(transport_url)
        samples: list[float] = []
        sock = ctx.socket(zmq.REQ)
        sock.connect(transport_url)
        for _ in range(n_warmup):
            sock.send(payload)
            sock.recv()
        for _ in range(n_iters):
            t0 = time.perf_counter()
            sock.send(payload)
            sock.recv()
            t1 = time.perf_counter()
            samples.append((t1 - t0) * 1e6)
        sock.close(linger=0)
        return samples

    # ------------------------------------------------------------------
    # Ops/sec
    # ------------------------------------------------------------------

    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        payload = bytes(msg_size)
        ctx = _get_ctx(transport_url)
        count = 0
        sock = ctx.socket(zmq.REQ)
        sock.connect(transport_url)
        deadline = time.perf_counter() + duration_s
        while time.perf_counter() < deadline:
            sock.send(payload)
            sock.recv()
            count += 1
        sock.close(linger=0)
        return count / duration_s


COMPETITORS["zmq_sync"] = ZmqSyncBenchmark

"""Benchmark competitor: ZeroMQ synchronous REQ/REP (pyzmq)."""

from __future__ import annotations

import threading
import time

import zmq

from .base import BaseBenchmark
from .._core.common import COMPETITORS

# ZMQ uses slightly different URL scheme:
# inproc needs a shared Context; ipc and tcp are identical to nng.
_ZMQ_INPROC_CTX: dict[str, zmq.Context] = {}
_CTX_LOCK = threading.Lock()


def _shared_inproc_ctx(url: str) -> zmq.Context | None:
    """Return a shared zmq.Context for inproc:// URLs (required by zmq)."""
    if url.startswith("inproc://"):
        with _CTX_LOCK:
            if url not in _ZMQ_INPROC_CTX:
                _ZMQ_INPROC_CTX[url] = zmq.Context()
            return _ZMQ_INPROC_CTX[url]
    return None


def _nng_url_to_zmq(url: str) -> str:
    """Convert nng URL format to zmq format (they are compatible for ipc/tcp)."""
    return url  # same format


class ZmqSyncBenchmark(BaseBenchmark):
    """Measures pyzmq with blocking send/recv."""

    name = "zmq_sync"

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    @staticmethod
    def _run_server(
        url: str,
        ctx: zmq.Context | None,
        ready: threading.Event,
        stop: threading.Event,
    ) -> None:
        own_ctx = ctx is None
        c = ctx if ctx is not None else zmq.Context()
        try:
            sock = c.socket(zmq.REP)
            sock.RCVTIMEO = 200  # ms
            sock.bind(url)
            ready.set()
            while not stop.is_set():
                try:
                    msg = sock.recv()
                    sock.send(msg)
                except zmq.Again:
                    pass
            sock.close(linger=0)
        finally:
            if own_ctx:
                c.term()

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
        shared_ctx = _shared_inproc_ctx(transport_url)
        ready = threading.Event()
        stop = threading.Event()

        server = threading.Thread(
            target=self._run_server,
            args=(transport_url, shared_ctx, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        own_ctx = shared_ctx is None
        ctx = shared_ctx if shared_ctx is not None else zmq.Context()
        samples: list[float] = []
        try:
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
        finally:
            if own_ctx:
                ctx.term()

        stop.set()
        server.join(timeout=2)
        with _CTX_LOCK:
            _ZMQ_INPROC_CTX.pop(transport_url, None)
        return samples

    # ------------------------------------------------------------------
    # Bandwidth
    # ------------------------------------------------------------------

    def measure_bandwidth(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        payload = bytes(msg_size)
        shared_ctx = _shared_inproc_ctx(transport_url)
        ready = threading.Event()
        stop = threading.Event()

        server = threading.Thread(
            target=self._run_server,
            args=(transport_url, shared_ctx, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        own_ctx = shared_ctx is None
        ctx = shared_ctx if shared_ctx is not None else zmq.Context()
        samples: list[float] = []
        try:
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
                samples.append(2 * msg_size / (t1 - t0) / 1e6)
            sock.close(linger=0)
        finally:
            if own_ctx:
                ctx.term()

        stop.set()
        server.join(timeout=2)
        with _CTX_LOCK:
            _ZMQ_INPROC_CTX.pop(transport_url, None)
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
        shared_ctx = _shared_inproc_ctx(transport_url)
        ready = threading.Event()
        stop = threading.Event()

        server = threading.Thread(
            target=self._run_server,
            args=(transport_url, shared_ctx, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        own_ctx = shared_ctx is None
        ctx = shared_ctx if shared_ctx is not None else zmq.Context()
        count = 0
        try:
            sock = ctx.socket(zmq.REQ)
            sock.connect(transport_url)
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                sock.send(payload)
                sock.recv()
                count += 1
            sock.close(linger=0)
        finally:
            if own_ctx:
                ctx.term()

        stop.set()
        server.join(timeout=2)
        with _CTX_LOCK:
            _ZMQ_INPROC_CTX.pop(transport_url, None)
        return count / duration_s


COMPETITORS["zmq_sync"] = ZmqSyncBenchmark

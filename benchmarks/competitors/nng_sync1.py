"""Benchmark competitor: python-nng synchronous REQ/REP via send/recv.

Blocking send/recv are the fastest way to send/receive messages.
Indeed they have the least overhead as they avoid the additional future/promise machinery,
and the use of internal nng threads for async submission and completion.

However be aware that when blocking, Python signals are not handled until the blocking call
returns. For that reason, prefer the alternatives, or use with care in a dedicated thread.
You can stop a blocking recv/send by closing the socket from another thread.
"""

from __future__ import annotations

import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS


class NngSyncBenchmark(BaseBenchmark):
    """Measures python-nng with blocking send/recv."""

    name = "nng_sync1"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()
        with nng.RepSocket() as rep:
            rep.recv_timeout = 200  # ms – check stop flag periodically
            rep.add_listener(url).start()
            ready.set()
            while not stop.is_set():
                try:
                    msg = rep.recv()
                    rep.send(b"")
                except nng.NngTimeout:
                    pass
                except nng.NngClosed:
                    break

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
        nng = import_nng()
        payload = bytes(msg_size)
        samples: list[float] = []
        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)
            # Warmup
            for _ in range(n_warmup):
                req.send(payload)
                req.recv()
            # Measured
            for _ in range(n_iters):
                t0 = time.perf_counter()
                req.send(payload)
                req.recv()
                t1 = time.perf_counter()
                samples.append((t1 - t0) * 1e6)
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
        nng = import_nng()
        payload = bytes(msg_size)
        count = 0
        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                req.send(payload)
                req.recv()
                count += 1
        return count / duration_s


COMPETITORS["nng_sync1"] = NngSyncBenchmark

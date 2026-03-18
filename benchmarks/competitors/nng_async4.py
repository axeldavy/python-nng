"""Benchmark competitor: python-nng REQ/REP via submit_send/submit_recv.

While asyncio simplifies handling communications in parallel to other tasks,
it is not required to do asynchronous send/recv with python-nng.

This demo uses submit_send/submit_recv, which return concurrent.futures.Future,
and blocks on .result().

This is similar to nng_sync2.py. The only minor difference is that
we do not wait synchronously for the sends to complete.

Note it is not possible to submit_send before receiving the reply,
as REP/REQ discards the previous message if you send a new one before
receiving the reply.
"""

from __future__ import annotations

import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS


class NngAsync4Benchmark(BaseBenchmark):
    """Measures python-nng with submit_send/submit_recv."""

    name = "nng_async4"

    # ------------------------------------------------------------------
    # Internal server (runs in its own thread, blocking on future.result())
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()
        with nng.RepSocket() as rep:
            rep.recv_timeout = 200
            rep.add_listener(url).start()
            ready.set()
            while not stop.is_set():
                try:
                    msg = rep.submit_recv().result()
                    rep.submit_send(b"")
                except nng.NngTimeout:
                    pass
                except nng.NngClosed:
                    break
                except Exception:
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
            for _ in range(n_warmup):
                req.submit_send(payload)
                req.submit_recv().result()
            for _ in range(n_iters):
                t0 = time.perf_counter()
                req.submit_send(payload)
                req.submit_recv().result()
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
                req.submit_send(payload)
                req.submit_recv().result()
                count += 1
        return count / duration_s


COMPETITORS["nng_async4"] = NngAsync4Benchmark

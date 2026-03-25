"""Benchmark competitor: python-nng synchronous REQ/REP via submit_send/submit_recv.

submit_send/submit_recv are an alternative way to do synchronous send/recv with python-nng.
They are slightly slower than blocking send/recv, but can be used to wait
later on completion.
"""

import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS


class NngSyncBenchmark(BaseBenchmark):
    """Measures python-nng with synchronous REQ/REP via submit_send/submit_recv."""

    name = "nng_sync2"

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
                    msg = rep.submit_recv().result()
                    rep.submit_send(b"").result()
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
                req.submit_send(payload).result()
                req.submit_recv().result()
            # Measured
            for _ in range(n_iters):
                t0 = time.perf_counter()
                req.submit_send(payload).result()
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
                req.submit_send(payload).result()
                req.submit_recv().result()
                count += 1
        return count / duration_s


COMPETITORS["nng_sync2"] = NngSyncBenchmark

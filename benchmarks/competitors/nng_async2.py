"""Benchmark competitor: python-nng async REQ/REP via asend/arecv.

asend/arecv are the recommended way to do asynchronous send/recv with python-nng.
They are slightly slower than arecv_ready/asend_ready, but have fewer potential
pitfalls to handle.
"""

import asyncio
import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS, run_in_new_loop


class NngAsyncBenchmark(BaseBenchmark):
    """Measures python-nng with async send/recv (asyncio)."""

    name = "nng_async2"

    # ------------------------------------------------------------------
    # Internal server (runs its own asyncio loop in a thread)
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()

        async def _serve() -> None:
            with nng.RepSocket() as rep:
                rep.recv_timeout = 200
                rep.add_listener(url).start()
                ready.set()
                while not stop.is_set():
                    try:
                        msg = await rep.arecv()
                        await rep.asend(b"")
                    except nng.NngTimeout:
                        pass
                    except nng.NngClosed:
                        break

        run_in_new_loop(_serve())

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

        async def _client() -> list[float]:
            samples: list[float] = []
            with nng.ReqSocket() as req:
                req.add_dialer(transport_url).start(block=True)
                for _ in range(n_warmup):
                    await req.asend(payload)
                    await req.arecv()
                for _ in range(n_iters):
                    t0 = time.perf_counter()
                    await req.asend(payload)
                    await req.arecv()
                    t1 = time.perf_counter()
                    samples.append((t1 - t0) * 1e6)
            return samples

        return run_in_new_loop(_client())

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

        async def _client() -> float:
            count = 0
            with nng.ReqSocket() as req:
                req.add_dialer(transport_url).start(block=True)
                deadline = time.perf_counter() + duration_s
                while time.perf_counter() < deadline:
                    await req.asend(payload)
                    await req.arecv()
                    count += 1
            return count / duration_s

        return run_in_new_loop(_client())


COMPETITORS["nng_async2"] = NngAsyncBenchmark

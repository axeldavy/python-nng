"""Benchmark competitor: pynng async REQ/REP.

This demo is similar to nng_async2.py, but uses
pynng's asyncio support instead of python-nng.

To this date, python-nng gets higher performance, mainly due
to compiled C++ (Cython) code for the asyncio machinery, while pynng is pure Python/ffi.
"""

import asyncio
import threading
import time

import pynng

from .base import BaseBenchmark
from .._core.common import COMPETITORS, run_in_new_loop


class PynngAsyncBenchmark(BaseBenchmark):
    """Measures pynng async send/recv (asyncio)."""

    name = "pynng_async"

    # ------------------------------------------------------------------
    # Server (asyncio)
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:

        async def _serve() -> None:
            with pynng.Rep0(listen=url, recv_timeout=200) as rep:
                ready.set()
                while not stop.is_set():
                    try:
                        msg = await rep.arecv()
                        await rep.asend(b" ")
                    except pynng.exceptions.Timeout:
                        pass

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
        payload = bytes(msg_size)

        async def _client() -> list[float]:
            samples: list[float] = []
            with pynng.Req0(dial=transport_url) as req:
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
        payload = bytes(msg_size)

        async def _client() -> float:
            count = 0
            with pynng.Req0(dial=transport_url) as req:
                deadline = time.perf_counter() + duration_s
                while time.perf_counter() < deadline:
                    await req.asend(payload)
                    await req.arecv()
                    count += 1
            return count / duration_s

        return run_in_new_loop(_client())


COMPETITORS["pynng_async"] = PynngAsyncBenchmark

"""Benchmark competitor: pynng async REQ/REP (trio or asyncio via anyio)."""

from __future__ import annotations

import asyncio
import threading
import time

import pynng

from .base import BaseBenchmark
from .._core.common import COMPETITORS

# pynng's native async uses trio. It ships an anyio-compatible interface
# via pynng.Socket.arecv()/asend() when called inside asyncio as well,
# using the asyncio backend of trio-asyncio or its own asyncio integration.
# We use pynng's asyncio-compatible wrappers directly: arecv()/asend().


class PynngAsyncBenchmark(BaseBenchmark):
    """Measures pynng async send/recv (asyncio)."""

    name = "pynng_async"

    # ------------------------------------------------------------------
    # Server (asyncio)
    # ------------------------------------------------------------------

    @staticmethod
    def _server_thread(url: str, ready: threading.Event, stop: threading.Event) -> None:
        async def _serve() -> None:
            with pynng.Rep0(listen=url, recv_timeout=200) as rep:
                ready.set()
                while not stop.is_set():
                    try:
                        msg = await rep.arecv()
                        await rep.asend(msg)
                    except pynng.exceptions.Timeout:
                        pass

        asyncio.run(_serve())

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
        ready = threading.Event()
        stop = threading.Event()

        server = threading.Thread(
            target=self._server_thread,
            args=(transport_url, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

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

        samples = asyncio.run(_client())
        stop.set()
        server.join(timeout=2)
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
        ready = threading.Event()
        stop = threading.Event()

        server = threading.Thread(
            target=self._server_thread,
            args=(transport_url, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

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
                    samples.append(2 * msg_size / (t1 - t0) / 1e6)
            return samples

        samples = asyncio.run(_client())
        stop.set()
        server.join(timeout=2)
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
        ready = threading.Event()
        stop = threading.Event()

        server = threading.Thread(
            target=self._server_thread,
            args=(transport_url, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        async def _client() -> float:
            count = 0
            with pynng.Req0(dial=transport_url) as req:
                deadline = time.perf_counter() + duration_s
                while time.perf_counter() < deadline:
                    await req.asend(payload)
                    await req.arecv()
                    count += 1
            return count / duration_s

        ops = asyncio.run(_client())
        stop.set()
        server.join(timeout=2)
        return ops


COMPETITORS["pynng_async"] = PynngAsyncBenchmark

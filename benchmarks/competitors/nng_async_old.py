"""Benchmark competitor: python-nng async REQ/REP via asyncio."""

from __future__ import annotations

import asyncio
import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS, get_new_event_loop


class NngAsyncBenchmark(BaseBenchmark):
    """Measures python-nng with async send/recv (asyncio)."""

    name = "nng_async_old"

    # ------------------------------------------------------------------
    # Internal server (runs its own asyncio loop in a thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _server_thread(url: str, ready: threading.Event, stop: threading.Event) -> None:
        nng = import_nng()
        asyncio.set_event_loop(get_new_event_loop())

        async def _serve() -> None:
            with nng.RepSocket() as rep:
                rep.recv_timeout = 200
                rep.add_listener(url).start()
                ready.set()
                while not stop.is_set():
                    try:
                        msg = await rep.arecv()
                        await rep.asend(msg)
                    except nng.NngTimeout:
                        pass
                    except nng.NngClosed:
                        break

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
        nng = import_nng()
        asyncio.set_event_loop(get_new_event_loop())
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
        nng = import_nng()
        asyncio.set_event_loop(get_new_event_loop())
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
                    elapsed = t1 - t0
                    samples.append(2 * msg_size / elapsed / 1e6)
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
        nng = import_nng()
        asyncio.set_event_loop(get_new_event_loop())
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
            with nng.ReqSocket() as req:
                req.add_dialer(transport_url).start(block=True)
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


COMPETITORS["nng_async_old"] = NngAsyncBenchmark

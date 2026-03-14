"""Benchmark competitor: pynng synchronous REQ/REP."""

from __future__ import annotations

import threading
import time

import pynng

from .base import BaseBenchmark
from .._core.common import COMPETITORS


class PynngSyncBenchmark(BaseBenchmark):
    """Measures pynng synchronous send/recv."""

    name = "pynng_sync"

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    @staticmethod
    def _run_server(url: str, ready: threading.Event, stop: threading.Event) -> None:
        with pynng.Rep0(listen=url, recv_timeout=200) as rep:
            ready.set()
            while not stop.is_set():
                try:
                    msg = rep.recv()
                    rep.send(msg)
                except pynng.exceptions.Timeout:
                    pass

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
            target=self._run_server,
            args=(transport_url, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        samples: list[float] = []
        with pynng.Req0(dial=transport_url) as req:
            for _ in range(n_warmup):
                req.send(payload)
                req.recv()
            for _ in range(n_iters):
                t0 = time.perf_counter()
                req.send(payload)
                req.recv()
                t1 = time.perf_counter()
                samples.append((t1 - t0) * 1e6)

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
            target=self._run_server,
            args=(transport_url, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        samples: list[float] = []
        with pynng.Req0(dial=transport_url) as req:
            for _ in range(n_warmup):
                req.send(payload)
                req.recv()
            for _ in range(n_iters):
                t0 = time.perf_counter()
                req.send(payload)
                req.recv()
                t1 = time.perf_counter()
                samples.append(2 * msg_size / (t1 - t0) / 1e6)

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
            target=self._run_server,
            args=(transport_url, ready, stop),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5)

        count = 0
        with pynng.Req0(dial=transport_url) as req:
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                req.send(payload)
                req.recv()
                count += 1

        stop.set()
        server.join(timeout=2)
        return count / duration_s


COMPETITORS["pynng_sync"] = PynngSyncBenchmark

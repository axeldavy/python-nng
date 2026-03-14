"""Benchmark competitor: python-nng synchronous REQ/REP via Contexts."""

from __future__ import annotations

import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS


class NngSyncBenchmark(BaseBenchmark):
    """Measures python-nng with blocking send/recv on a Context."""

    name = "nng_sync"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_server(url: str, ready: threading.Event, stop: threading.Event) -> None:
        nng = import_nng()
        with nng.RepSocket() as rep:
            rep.recv_timeout = 200  # ms – check stop flag periodically
            rep.add_listener(url).start()
            ready.set()
            while not stop.is_set():
                try:
                    msg = rep.recv()
                    rep.send(msg)
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
        """Bandwidth = msg_size / RTT per round-trip (MB/s)."""
        nng = import_nng()
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
        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)
            for _ in range(n_warmup):
                req.send(payload)
                req.recv()
            for _ in range(n_iters):
                t0 = time.perf_counter()
                req.send(payload)
                req.recv()
                t1 = time.perf_counter()
                elapsed = t1 - t0
                # Two message payloads travel: request + response
                samples.append(2 * msg_size / elapsed / 1e6)

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
        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                req.send(payload)
                req.recv()
                count += 1

        stop.set()
        server.join(timeout=2)
        return count / duration_s


COMPETITORS["nng_sync"] = NngSyncBenchmark

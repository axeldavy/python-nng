"""Benchmark competitor: ZeroMQ async REQ/REP via zmq.asyncio (pyzmq)."""

from __future__ import annotations

import asyncio
import threading
import time

import zmq
import zmq.asyncio as azmq

from .base import BaseBenchmark
from .._core.common import COMPETITORS, get_new_event_loop

# zmq.asyncio requires an asyncio event loop to be running. For inproc the
# context must be shared between server and client coroutines — both run in
# the same asyncio loop so we create one shared azmq.Context per benchmark
# run (not per URL, since they already live in the same loop).


class ZmqAsyncBenchmark(BaseBenchmark):
    """Measures pyzmq async (zmq.asyncio) with DEALER/ROUTER for full async."""

    name = "zmq_async"

    # We use DEALER (async REQ) / ROUTER (async REP) to avoid the strict
    # send-recv lock-step of REQ/REP which doesn't compose well with asyncio.

    @staticmethod
    async def _serve(url: str, ctx: azmq.Context, ready: asyncio.Event, stop: asyncio.Event) -> None:
        sock = ctx.socket(zmq.ROUTER)
        sock.RCVTIMEO = -1  # non-blocking via asyncio
        sock.bind(url)
        ready.set()
        while not stop.is_set():
            try:
                parts = await asyncio.wait_for(sock.recv_multipart(), timeout=0.2)
                # ROUTER frame: [identity, empty, payload]
                await sock.send_multipart(parts)
            except asyncio.TimeoutError:
                pass
        sock.close(linger=0)

    # ------------------------------------------------------------------
    # Shared runner that sets up one asyncio loop for server+client
    # ------------------------------------------------------------------

    @staticmethod
    async def _run(
        url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
        mode: str,       # "latency" | "bandwidth"
        duration_s: float = 5.0,
    ) -> list[float] | float:
        ctx = azmq.Context()
        ready = asyncio.Event()
        stop = asyncio.Event()

        server_task = asyncio.create_task(
            ZmqAsyncBenchmark._serve(url, ctx, ready, stop)
        )
        await ready.wait()

        payload = bytes(msg_size)
        sock = ctx.socket(zmq.DEALER)
        sock.connect(url)

        samples: list[float] = []

        if mode in ("latency", "bandwidth"):
            for _ in range(n_warmup):
                await sock.send_multipart([b"", payload])
                await sock.recv_multipart()
            for _ in range(n_iters):
                t0 = time.perf_counter()
                await sock.send_multipart([b"", payload])
                await sock.recv_multipart()
                t1 = time.perf_counter()
                if mode == "latency":
                    samples.append((t1 - t0) * 1e6)
                else:
                    samples.append(2 * msg_size / (t1 - t0) / 1e6)
        else:  # ops
            count = 0
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                await sock.send_multipart([b"", payload])
                await sock.recv_multipart()
                count += 1
            sock.close(linger=0)
            stop.set()
            await server_task
            ctx.term()
            return count / duration_s

        sock.close(linger=0)
        stop.set()
        await server_task
        ctx.term()
        return samples

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _run_in_loop(self, coro) -> list[float] | float:
        asyncio.set_event_loop(get_new_event_loop())
        return asyncio.run(coro)

    def measure_latency(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        return self._run_in_loop(  # type: ignore[return-value]
            self._run(transport_url, msg_size, n_warmup, n_iters, "latency")
        )

    def measure_bandwidth(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        return self._run_in_loop(  # type: ignore[return-value]
            self._run(transport_url, msg_size, n_warmup, n_iters, "bandwidth")
        )

    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        return self._run_in_loop(  # type: ignore[return-value]
            self._run(transport_url, msg_size, 0, 0, "ops", duration_s)
        )


COMPETITORS["zmq_async"] = ZmqAsyncBenchmark

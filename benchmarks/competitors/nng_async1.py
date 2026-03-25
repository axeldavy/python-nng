"""Benchmark competitor: python-nng async REQ/REP via arecv_ready/asend_ready.

arecv_ready/asend_ready are the fastest way to do asynchronous send/recv with python-nng.
They enable to wait for the socket to be ready for sending/receiving, and then do a
non-blocking send/recv.

The reason for this superior performance is that arecv/asend involve latency by message
passing in two internal threads.
It remains slower than recv/send as it involves a bit of overhead for the
asyncio Future/Promise machinery.

A pitfall to be aware of is that while arecv_ready/asend_ready wake when they
detect the socket is ready, it might not be ready any more by the time you
actually perform the send/recv operation. For instance if the target peer
has received work from another peer, or if you have used the same socket
in another thread.
For these reasons, it is safer to use blocking recv/send after waiting
for arecv_ready/asend_ready, to avoid having to handle EAGAIN errors.
This is what the benchmark does, but means you may block your asyncio loop.

As a result, if you may hit any of these in your applications prefer asend/arecv.

Note arecv_reader/asend_ready apply to sockets only and not contexts, thus
when using contexts, you should use asend/arecv instead.

"""

import asyncio
import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS, run_in_new_loop


class NngAsyncBenchmark(BaseBenchmark):
    """Measures python-nng with async send/recv (asyncio)."""

    name = "nng_async1"

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
                arecv_ready = rep.arecv_ready()
                asend_ready = rep.asend_ready()
                while not stop.is_set():
                    try:
                        await anext(arecv_ready)
                        msg = rep.recv()
                        await anext(asend_ready)
                        rep.send(b"")
                    except nng.NngTimeout:
                        pass
                    except nng.NngClosed:
                        break
                await arecv_ready.aclose()
                await asend_ready.aclose()

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
                asend_ready = req.asend_ready()
                arecv_ready = req.arecv_ready()
                for _ in range(n_warmup):
                    await anext(asend_ready)
                    req.send(payload)
                    await anext(arecv_ready)
                    msg = req.recv()
                for _ in range(n_iters):
                    t0 = time.perf_counter()
                    await anext(asend_ready)
                    req.send(payload)
                    await anext(arecv_ready)
                    msg = req.recv()
                    t1 = time.perf_counter()
                    samples.append((t1 - t0) * 1e6)
                await arecv_ready.aclose()
                await asend_ready.aclose()
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
                asend_ready = req.asend_ready()
                arecv_ready = req.arecv_ready()
                while time.perf_counter() < deadline:
                    await anext(asend_ready)
                    req.send(payload, nonblock=True)
                    await anext(arecv_ready)
                    msg = req.recv(nonblock=True)
                    count += 1
                await arecv_ready.aclose()
                await asend_ready.aclose()
            return count / duration_s

        return run_in_new_loop(_client())


COMPETITORS["nng_async1"] = NngAsyncBenchmark

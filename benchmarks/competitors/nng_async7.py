"""Benchmark competitor: python-nng threaded REQ/REP with N parallel contexts.

Unlike nng_async3 which uses asyncio coroutines, this variant drives N
independent contexts in N threads — one blocking send/recv loop per thread.
Each thread owns one context on a shared socket, so N requests are in flight
simultaneously without any event loop.
"""

import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS

# ---------------------------------------------------------------------------
# Parallelism knob
# ---------------------------------------------------------------------------
N_CONTEXTS: int = 10  # number of concurrent client/server context threads


class NngAsync7Benchmark(BaseBenchmark):
    """Measures python-nng throughput with N_CONTEXTS parallel threaded contexts."""

    name = "nng_async7"

    # ------------------------------------------------------------------
    # Internal server
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()

        def ctx_loop(ctx: object) -> None:
            ctx.recv_timeout = 200  # ms – poll so we notice *stop*
            while not stop.is_set():
                try:
                    ctx.recv()
                    ctx.send(b"")
                except nng.NngTimeout:
                    pass
                except nng.NngClosed:
                    break

        with nng.RepSocket() as rep:
            rep.add_listener(url).start()
            ready.set()

            threads = [
                threading.Thread(target=ctx_loop, args=(rep.open_context(),), daemon=True)
                for _ in range(N_CONTEXTS)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

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

        warmup_each = max(1, n_warmup // N_CONTEXTS)
        iters_each = max(1, n_iters // N_CONTEXTS)

        results: list[float] = []
        lock = threading.Lock()

        def ctx_worker(ctx: object) -> None:
            # Warmup
            for _ in range(warmup_each):
                ctx.send(payload)
                ctx.recv()

            # Measured: amortise over block so parallel contexts don't inflate each other's RTT
            t0 = time.perf_counter()
            for _ in range(iters_each):
                ctx.send(payload)
                ctx.recv()
            t1 = time.perf_counter()
            sample = (t1 - t0) / iters_each * 1e6  # µs per round-trip

            with lock:
                results.append(sample)

        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)

            threads = [
                threading.Thread(target=ctx_worker, args=(req.open_context(),))
                for _ in range(N_CONTEXTS)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        return results

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

        counts: list[int] = []
        lock = threading.Lock()

        def ctx_worker(ctx: object) -> None:
            count = 0
            deadline = time.perf_counter() + duration_s
            while time.perf_counter() < deadline:
                ctx.send(payload)
                ctx.recv()
                count += 1
            with lock:
                counts.append(count)

        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)

            threads = [
                threading.Thread(target=ctx_worker, args=(req.open_context(),))
                for _ in range(N_CONTEXTS)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        return sum(counts) / duration_s


COMPETITORS["nng_async7"] = NngAsync7Benchmark

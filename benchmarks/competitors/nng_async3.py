"""Benchmark competitor: python-nng async REQ/REP with N parallel contexts.

REQ/REP are limited to one message in flight at a time. This example
to achieve higher throughput uses N parallel contexts to queue more
messages in flight, and thus achieve higher throughput at the cost of higher latency.
"""

import asyncio
import threading
import time

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS, run_in_new_loop

# ---------------------------------------------------------------------------
# Parallelism knob
# ---------------------------------------------------------------------------
N_CONTEXTS: int = 10  # number of concurrent client contexts (and server contexts)


class NngAsync3Benchmark(BaseBenchmark):
    """Measures python-nng throughput with N_CONTEXTS parallel REQ/REP contexts."""

    name = "nng_async3"

    # ------------------------------------------------------------------
    # Internal server
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()

        async def _serve() -> None:
            with nng.RepSocket() as rep:
                rep.add_listener(url).start()
                ready.set()

                async def ctx_loop(ctx: object) -> None:
                    ctx.recv_timeout = 200  # ms – poll so we notice *stop*
                    while not stop.is_set():
                        try:
                            msg = await ctx.arecv()
                            await ctx.asend(b"")
                        except nng.NngTimeout:
                            pass
                        except nng.NngClosed:
                            break

                async with asyncio.TaskGroup() as tg:
                    for _ in range(N_CONTEXTS):
                        tg.create_task(ctx_loop(rep.open_context()))

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

        warmup_each = max(1, n_warmup // N_CONTEXTS)
        iters_each = max(1, n_iters // N_CONTEXTS)

        async def _client() -> list[float]:
            with nng.ReqSocket() as req:
                req.add_dialer(transport_url).start(block=True)

                # --- warmup ---
                async def warmup_ctx(ctx: object) -> None:
                    for _ in range(warmup_each):
                        await ctx.asend(payload)
                        await ctx.arecv()

                async with asyncio.TaskGroup() as tg:
                    for _ in range(N_CONTEXTS):
                        tg.create_task(warmup_ctx(req.open_context()))

                # --- measurement ---
                # Each context times its own block of iters_each round-trips and
                # contributes ONE amortized sample = block_time / iters_each.
                # This avoids the artefact where per-RTT timings skyrocket because
                # every context queues behind all the others.
                async def measure_ctx(ctx: object) -> float:
                    t0 = time.perf_counter()
                    for _ in range(iters_each):
                        await ctx.asend(payload)
                        await ctx.arecv()
                    t1 = time.perf_counter()
                    return (t1 - t0) / iters_each * 1e6  # µs per request

                tasks: list[asyncio.Task] = []
                async with asyncio.TaskGroup() as tg:
                    for _ in range(N_CONTEXTS):
                        tasks.append(tg.create_task(measure_ctx(req.open_context())))

            # One sample per context; total = N_CONTEXTS samples.
            return [task.result() for task in tasks]

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
            with nng.ReqSocket() as req:
                req.add_dialer(transport_url).start(block=True)
                deadline = time.perf_counter() + duration_s

                async def ctx_loop(ctx: object) -> int:
                    count = 0
                    while time.perf_counter() < deadline:
                        await ctx.asend(payload)
                        await ctx.arecv()
                        count += 1
                    return count

                tasks: list[asyncio.Task] = []
                async with asyncio.TaskGroup() as tg:
                    for _ in range(N_CONTEXTS):
                        tasks.append(tg.create_task(ctx_loop(req.open_context())))

            total = sum(task.result() for task in tasks)
            return total / duration_s

        return run_in_new_loop(_client())


COMPETITORS["nng_async3"] = NngAsync3Benchmark

"""Benchmark competitor: python-nng REQ/REP via concurrent.futures with add_done_callback chaining.

This variant of nng_async4 replaces the blocking loop with a callback chain
where each operation completion is wired to the next via ``add_done_callback``:

    submit_recv() → on_recv_done → submit_send() → on_send_done → submit_recv() → …

No event loop and no blocking waits are used on the critical path; a
``threading.Event`` is only used at the outer measurement boundary to know
when the chain has finished.
"""

import threading
import time
from concurrent.futures import Future

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS


class NngAsync5Benchmark(BaseBenchmark):
    """Measures python-nng with submit_send/submit_recv chained via add_done_callback."""

    name = "nng_async5"

    # ------------------------------------------------------------------
    # Internal server (callback-chain driven, blocks on chain_done)
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()
        chain_done = threading.Event()

        with nng.RepSocket() as rep:
            rep.recv_timeout = 200
            rep.add_listener(url).start()
            ready.set()

            def on_send_done(fut: Future) -> None:
                try:
                    fut.result()
                except Exception:
                    chain_done.set()
                    return
                if stop.is_set():
                    chain_done.set()
                    return
                rep.submit_recv().add_done_callback(on_recv_done)

            def on_recv_done(fut: Future) -> None:
                try:
                    msg = fut.result()
                except nng.NngTimeout:
                    if stop.is_set():
                        chain_done.set()
                        return
                    # Timeout but still running – re-arm recv
                    rep.submit_recv().add_done_callback(on_recv_done)
                    return
                except nng.NngClosed:
                    chain_done.set()
                    return
                except Exception:
                    chain_done.set()
                    return
                rep.submit_send(b"").add_done_callback(on_send_done)

            # Kick off the recv chain
            rep.submit_recv().add_done_callback(on_recv_done)
            chain_done.wait()

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
        samples: list[float] = []
        done = threading.Event()
        # remaining[0] counts how many (warmup + measurement) round-trips are left
        remaining = [n_warmup + n_iters]
        t_start: list[float] = [0.0]

        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)

            def on_send_done(fut: Future) -> None:
                try:
                    fut.result()
                except Exception:
                    done.set()
                    return
                req.submit_recv().add_done_callback(on_recv_done)

            def on_recv_done(fut: Future) -> None:
                try:
                    fut.result()
                except Exception:
                    done.set()
                    return

                t1 = time.perf_counter()
                # Determine which iteration this is (0-indexed from the start)
                iteration = n_warmup + n_iters - remaining[0]
                if iteration >= n_warmup:
                    samples.append((t1 - t_start[0]) * 1e6)

                remaining[0] -= 1
                if remaining[0] <= 0:
                    done.set()
                    return

                # Next iteration: arm the send
                t_start[0] = time.perf_counter()
                req.submit_send(payload).add_done_callback(on_send_done)

            # Kick off the first send
            t_start[0] = time.perf_counter()
            req.submit_send(payload).add_done_callback(on_send_done)
            done.wait()

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
        count: list[int] = [0]
        done = threading.Event()
        deadline = time.perf_counter() + duration_s

        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)

            def on_send_done(fut: Future) -> None:
                try:
                    fut.result()
                except Exception:
                    done.set()
                    return
                req.submit_recv().add_done_callback(on_recv_done)

            def on_recv_done(fut: Future) -> None:
                try:
                    fut.result()
                except Exception:
                    done.set()
                    return
                count[0] += 1
                if time.perf_counter() >= deadline:
                    done.set()
                    return
                req.submit_send(payload).add_done_callback(on_send_done)

            req.submit_send(payload).add_done_callback(on_send_done)
            done.wait()

        return count[0] / duration_s


COMPETITORS["nng_async5"] = NngAsync5Benchmark

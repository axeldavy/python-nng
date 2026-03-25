"""Benchmark competitor: python-nng REQ/REP with N parallel contexts, callback-chained.

Combines nng_async3 (N_CONTEXTS parallel REQ/REP contexts) with nng_async5
(callback chaining via ``add_done_callback`` instead of asyncio).

N_CONTEXTS independent REQ contexts run simultaneously, each driving its own
send → recv → send → … chain entirely through nng's AIO thread pool.  No
event loop is involved.

Note this is mostly a benchmark for "I wonder how it would do if ...", and
it is not how you should use python-nng. Indeed the callbacks will run
on our internal dispatch thread, which you don't want to block with user code.
"""

import threading
import time
from concurrent.futures import Future

from .._core.nng_import import import_nng
from .base import BaseBenchmark
from .._core.common import COMPETITORS

# ---------------------------------------------------------------------------
# Parallelism knob – mirror nng_async3
# ---------------------------------------------------------------------------
N_CONTEXTS: int = 10


class NngAsync6Benchmark(BaseBenchmark):
    """Measures python-nng with N_CONTEXTS parallel contexts, driven by add_done_callback."""

    name = "nng_async6"

    # ------------------------------------------------------------------
    # Internal server – N_CONTEXTS contexts, each in its own callback chain
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        nng = import_nng()
        lock = threading.Lock()
        done_count = [0]
        all_done = threading.Event()

        def _finish_ctx() -> None:
            with lock:
                done_count[0] += 1
                if done_count[0] == N_CONTEXTS:
                    all_done.set()

        def _make_chain(ctx: object):
            def on_send_done(fut: Future) -> None:
                try:
                    fut.result()
                except Exception:
                    _finish_ctx()
                    return
                if stop.is_set():
                    _finish_ctx()
                    return
                ctx.submit_recv().add_done_callback(on_recv_done)

            def on_recv_done(fut: Future) -> None:
                try:
                    msg = fut.result()
                except nng.NngTimeout:
                    if stop.is_set():
                        _finish_ctx()
                        return
                    # Timed out but still running – re-arm recv
                    ctx.submit_recv().add_done_callback(on_recv_done)
                    return
                except nng.NngClosed:
                    _finish_ctx()
                    return
                except Exception:
                    _finish_ctx()
                    return
                ctx.submit_send(b"").add_done_callback(on_send_done)

            return on_recv_done  # entry point

        with nng.RepSocket() as rep:
            rep.add_listener(url).start()
            ready.set()

            for _ in range(N_CONTEXTS):
                ctx = rep.open_context()
                ctx.recv_timeout = 200  # ms – allows noticing *stop*
                on_recv_done = _make_chain(ctx)
                ctx.submit_recv().add_done_callback(on_recv_done)

            all_done.wait()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_latency_chain(
        ctx: object,
        payload: bytes,
        warmup_each: int,
        iters_each: int,
        lock: threading.Lock,
        samples: list,
        done_count: list,
        all_done: threading.Event,
        nng: object,
    ):
        state = {"remaining": warmup_each + iters_each, "t_block_start": None}

        def _finish_ctx(sample: float | None = None) -> None:
            if sample is not None:
                with lock:
                    samples.append(sample)
            with lock:
                done_count[0] += 1
                if done_count[0] == N_CONTEXTS:
                    all_done.set()

        def _submit_send() -> None:
            # Start the block timer just before the first measurement send
            if state["remaining"] == iters_each and state["t_block_start"] is None:
                state["t_block_start"] = time.perf_counter()
            ctx.submit_send(payload).add_done_callback(on_send_done)

        def on_send_done(fut: Future) -> None:
            try:
                fut.result()
            except Exception:
                _finish_ctx()
                return
            ctx.submit_recv().add_done_callback(on_recv_done)

        def on_recv_done(fut: Future) -> None:
            try:
                fut.result()
            except Exception:
                _finish_ctx()
                return

            state["remaining"] -= 1

            if state["remaining"] <= 0:
                t1 = time.perf_counter()
                elapsed = t1 - state["t_block_start"]
                _finish_ctx(elapsed / iters_each * 1e6)
                return

            _submit_send()

        return _submit_send  # caller invokes this to kick off the chain

    @staticmethod
    def _make_bandwidth_chain(
        ctx: object,
        payload: bytes,
        msg_size: int,
        warmup_each: int,
        iters_each: int,
        lock: threading.Lock,
        samples: list,
        done_count: list,
        all_done: threading.Event,
        nng: object,
    ):
        state = {"remaining": warmup_each + iters_each, "t_block_start": None}

        def _finish_ctx(sample: float | None = None) -> None:
            if sample is not None:
                with lock:
                    samples.append(sample)
            with lock:
                done_count[0] += 1
                if done_count[0] == N_CONTEXTS:
                    all_done.set()

        def _submit_send() -> None:
            if state["remaining"] == iters_each and state["t_block_start"] is None:
                state["t_block_start"] = time.perf_counter()
            ctx.submit_send(payload).add_done_callback(on_send_done)

        def on_send_done(fut: Future) -> None:
            try:
                fut.result()
            except Exception:
                _finish_ctx()
                return
            ctx.submit_recv().add_done_callback(on_recv_done)

        def on_recv_done(fut: Future) -> None:
            try:
                fut.result()
            except Exception:
                _finish_ctx()
                return

            state["remaining"] -= 1

            if state["remaining"] <= 0:
                t1 = time.perf_counter()
                elapsed = t1 - state["t_block_start"]
                # Total bytes through the system during this measurement block:
                # all N_CONTEXTS contexts ran iters_each messages each way (×2).
                total_bytes = N_CONTEXTS * iters_each * 2 * msg_size
                _finish_ctx(total_bytes / elapsed / 1e6)
                return

            _submit_send()

        return _submit_send

    @staticmethod
    def _make_ops_chain(
        ctx: object,
        payload: bytes,
        deadline: float,
        lock: threading.Lock,
        count: list,
        done_count: list,
        all_done: threading.Event,
        nng: object,
    ):
        def _finish_ctx() -> None:
            with lock:
                done_count[0] += 1
                if done_count[0] == N_CONTEXTS:
                    all_done.set()

        def on_send_done(fut: Future) -> None:
            try:
                fut.result()
            except Exception:
                _finish_ctx()
                return
            ctx.submit_recv().add_done_callback(on_recv_done)

        def on_recv_done(fut: Future) -> None:
            try:
                fut.result()
            except Exception:
                _finish_ctx()
                return
            with lock:
                count[0] += 1
            if time.perf_counter() >= deadline:
                _finish_ctx()
                return
            ctx.submit_send(payload).add_done_callback(on_send_done)

        def kickoff() -> None:
            ctx.submit_send(payload).add_done_callback(on_send_done)

        return kickoff

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

        samples: list[float] = []
        lock = threading.Lock()
        done_count = [0]
        all_done = threading.Event()

        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)

            for _ in range(N_CONTEXTS):
                ctx = req.open_context()
                kickoff = self._make_latency_chain(
                    ctx, payload, warmup_each, iters_each,
                    lock, samples, done_count, all_done, nng,
                )
                kickoff()

            all_done.wait()

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
        lock = threading.Lock()
        done_count = [0]
        all_done = threading.Event()
        deadline = time.perf_counter() + duration_s

        with nng.ReqSocket() as req:
            req.add_dialer(transport_url).start(block=True)

            for _ in range(N_CONTEXTS):
                ctx = req.open_context()
                kickoff = self._make_ops_chain(
                    ctx, payload, deadline,
                    lock, count, done_count, all_done, nng,
                )
                kickoff()

            all_done.wait()

        return count[0] / duration_s


COMPETITORS["nng_async6"] = NngAsync6Benchmark

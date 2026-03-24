#!/usr/bin/env python3
"""PULL-PUSH inproc thread pool with a ThreadPoolExecutor-style interface.

This is a toy example, do not use this outside of this educational demo.

Architecture
------------

    ┌──────────────────────────────────────────────────┐
    │  Main thread                                     │
    │                                                  │
    │   pool.submit(fn, *args) ──► PushSocket          │
    │     stores (future, fn, args) in _pending        │
    │     sends 8-byte call_id ──────────┐ inproc://   │
    │                       ┌───────────┴──────────┐   │
    │                       ▼           ▼          ▼   │
    │                 PullSocket   PullSocket  PullSocket│
    │                 [worker-0]  [worker-1]  [worker-2]│
    │                     │           │           │    │
    │                     └─────┬─────┘           │    │
    │                           ▼                 ▼    │
    │              look up (fn, args) in _pending      │
    │                      fn(*args)          fn(*args) │
    │                           │                 │    │
    │                     future.set_result()          │
    └──────────────────────────────────────────────────┘

All N workers share *one* inproc endpoint. nng's PUSH socket sends each
message to exactly one PULL socket in a round-robin manner, acting as a
built-in work queue — no explicit queue object is needed.

Work items are stored in a shared dict keyed by an auto-incrementing
call-id. Only that 8-byte integer is sent over the wire on this demo.

Graceful shutdown is achieved by closing the push socket. We catch connection
loss and close the workers context, which causes ``recv()``
to raise an exception, which the worker catches and uses as its exit signal.
"""

from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future
import math
import os
import struct
import threading
import time
from typing import Any, TypeVar
import uuid

import nng

# 8-byte unsigned integer.
_ID_PACK = struct.Struct("<Q")

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# Thread pool
# ─────────────────────────────────────────────────────────────────────────────

class NngThreadPool:
    """A fixed-size thread pool backed by an nng PUSH-PULL inproc channel.

    Parameters
    ----------
    num_workers:
        Number of worker threads. Defaults to ``os.cpu_count()``.
    url:
        inproc URL used for the internal channel. A unique URL is generated
        automatically if not provided.
    """

    def __init__(
        self,
        num_workers: int | None = None,
        url: str | None = None,
    ) -> None:
        if num_workers is None:
            num_workers = os.cpu_count() or 4
        if url is None:
            url = f"inproc://nng-pool-{uuid.uuid4().hex[:8]}"

        self._url = url
        self._num_workers = num_workers
        self._shutdown = False

        # Thread-safe bookkeeping: maps call_id → (future, fn, args, kwargs).
        # The worker pops its entry, so no separate futures dict is needed.
        self._next_id_lock = threading.Lock()
        self._next_id: int = 0
        self._pending: dict[int, tuple[Future[Any], Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = {}

        # The push socket is shared across the main thread only.
        # A separate lock guards it so submit() is thread-safe.
        self._push = nng.PushSocket()
        self._push.add_listener(url).start()

        # Allow pending command buffering (up to 8192 commands)
        # The default (0) means that send is blocking until at least
        # a worker does a recv (unbuffered).
        self._push.send_buf = 8192

        # Spawn worker threads.
        self._workers: list[threading.Thread] = []
        for idx in range(num_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"nng-pool-worker-{idx}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """Each worker owns one PullSocket and blocks waiting for messages."""
        with nng.PullSocket() as pull:
            dialer = pull.add_dialer(self._url)
            dialer.start(block=True)

            # After a successful blocking start the pipe is live.
            # Register a status-change callback: when the push side closes
            # the pipe transitions to REMOVED, at which point we close the
            # pull socket so that the pending recv() raises NngClosed.

            pipe = dialer.pipe
            assert pipe is not None  # blocking start

            def _on_pipe_status_change() -> None:
                if pipe.status == nng.PipeStatus.REMOVED:
                    pull.close()

            pipe.on_status_change = _on_pipe_status_change

            # Fetch then run work items until the pipe is closed
            while True:
                try:
                    msg = pull.recv()  # nng.Message
                except Exception:
                    # Pull socket was closed by the pipe callback → exit.
                    return

                (call_id,) = _ID_PACK.unpack(msg)

                # Note: dict is threadsafe for pop on nogil.
                entry = self._pending.pop(call_id, None)

                if entry is None:
                    continue  # cancel_futures=True may have removed this item already

                # Run the work item and set the future's result or exception.
                future, fn, args, kwargs = entry
                if future.cancelled():
                    continue

                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(
        self, fn: Callable[..., T], /, *args: Any, **kwargs: Any
    ) -> "Future[T]":
        """Schedule *fn* to run in the pool.

        Returns a :class:`~concurrent.futures.Future` whose ``.result()``
        will hold the return value once the work item has been executed.

        This method is **thread-safe**.
        """
        if self._shutdown:
            raise RuntimeError("Cannot submit work after shutdown() has been called")

        future: Future[Any] = Future()
        with self._next_id_lock:
            call_id = self._next_id
            self._next_id += 1

        self._pending[call_id] = (future, fn, args, kwargs)

        # Pack message
        msg = _ID_PACK.pack(call_id)

        # Append the call_id to the push queue.
        self._push.send(msg)

        return future  # type: ignore[return-value]

    def map(
        self,
        fn: Callable[..., T],
        *iterables: Iterable[Any],
        timeout: float | None = None,
        buffersize = None
    ) -> Iterator[T]:
        """Map *fn* over *iterables* and yield results **in submission order**.

        This mirrors :meth:`concurrent.futures.Executor.map`:

        * All items are submitted immediately.
        * Results are yielded as each future completes **in order**, so a slow
          early item will delay later ones even if they finished first.
        """
        # buffersize limits the number of in-flight tasks (i.e. queued + running)
        if buffersize is not None:
            self._push.send_buf = max(0, buffersize - self._num_workers)
        else:
            buffersize = 8192 # PushSocket has a maximum send buffer of 8192

        futures = deque()
        for args in zip(*iterables):
            futures.append(self.submit(fn, *args))
            if len(futures) >= buffersize:
                yield futures.popleft().result(timeout=timeout)

        for f in futures:
            yield f.result(timeout=timeout)
    
        # Restore default
        self._push.send_buf = 8192

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Signal all workers to stop and (optionally) wait for them.

        Closing the push socket causes every worker's pending ``recv()``
        to raise an exception, which each worker interprets as its exit signal.

        After ``shutdown()`` returns, the pool is no longer usable.
        """
        if self._shutdown:
            return
        self._shutdown = True

        # Remove pending work items and cancel their futures.
        if cancel_futures:
            while True:
                # Remove item from last inserted to first.
                # Note: popitem is threadsafe for nogil.
                try:
                    _, (future, _, _, _) = self._pending.popitem()
                    future.cancel()
                except KeyError:
                    break # self._pending is now empty

        # Closing the push socket unblocks all workers at once.
        self._push.close()

        # Wait threads join
        if wait:
            for t in self._workers:
                t.join()

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "NngThreadPool":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.shutdown(wait=True)
        return False  # do not suppress exceptions


# ─────────────────────────────────────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────────────────────────────────────

def _slow_square(n: int) -> int:
    """Simulate a CPU-bound task by sleeping briefly, then squaring *n*."""
    time.sleep(0.05)
    return n * n


def main() -> None:
    num_workers = 4
    num_tasks = 12

    print(f"NngThreadPool demo — {num_workers} workers, {num_tasks} tasks\n")

    # ── Example 1: submit() → Future ─────────────────────────────────────────
    print("── submit() ──────────────────────────────────────────────────────")
    with NngThreadPool(num_workers=num_workers) as pool:
        futures = {n: pool.submit(_slow_square, n) for n in range(num_tasks)}

        for n, f in futures.items():
            result = f.result()
            print(f"  square({n:2d}) = {result:4d}")

    # ── Example 2: map() ─────────────────────────────────────────────────────
    print()
    print("── map() ─────────────────────────────────────────────────────────")
    inputs = list(range(num_tasks))

    t0 = time.perf_counter()
    with NngThreadPool(num_workers=num_workers) as pool:
        results = list(pool.map(math.factorial, inputs))
    elapsed = time.perf_counter() - t0

    for n, r in zip(inputs, results):
        print(f"  {n:2d}! = {r}")
    print(f"\n  Computed {num_tasks} factorials in {elapsed:.3f}s "
          f"(~{elapsed / num_tasks * 1000:.1f} ms each)\n")

    # ── Example 3: exception propagation ─────────────────────────────────────
    print("── exception propagation ─────────────────────────────────────────")
    with NngThreadPool(num_workers=2) as pool:
        good = pool.submit(int, "42")
        bad  = pool.submit(int, "not-a-number")

        print(f"  good result : {good.result()}")
        try:
            bad.result()
        except ValueError as exc:
            print(f"  caught expected exception: {exc}")


if __name__ == "__main__":
    main()

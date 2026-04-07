"""python_raw_async — raw Python baseline competitor (asyncio).

Measures the absolute floor of communication latency/throughput using nothing
but the Python standard library:

* **inproc** — a pair of :class:`queue.Queue` objects shared via a module-level
  dict, keyed by URL.  The server thread loops with ``get(timeout=0.2)`` so
  that it notices the ``stop`` event without blocking forever.

* **ipc** — Unix domain socket streams via :mod:`asyncio` (``start_unix_server``
  / ``open_unix_connection``).  The server cleans up the socket file before
  binding and after shutdown.  **Not supported on Windows** — methods raise
  :exc:`NotImplementedError` on that platform.

* **tcp** — :mod:`asyncio` stream server / client.  Fixed-size framing:
  the server echoes back exactly ``msg_size`` bytes; the client uses
  ``reader.readexactly(msg_size)`` so no framing overhead is added.
"""

import asyncio
import os
import queue
import sys
import threading
from time import perf_counter

from .base import BaseBenchmark
from .._core.common import COMPETITORS

# IPC (Unix domain sockets) is not available on Windows.
_IPC_SUPPORTED: bool = sys.platform != "win32"

# ---------------------------------------------------------------------------
# Shared inproc state
# ---------------------------------------------------------------------------

# URL → (req_queue, resp_queue)
_INPROC_QUEUES: dict[str, tuple[queue.Queue, queue.Queue]] = {}
_INPROC_LOCK = threading.Lock()


def _get_inproc_queues(url: str) -> tuple[queue.Queue, queue.Queue]:
    """Return existing queues for *url*, creating them if necessary."""
    with _INPROC_LOCK:
        if url not in _INPROC_QUEUES:
            _INPROC_QUEUES[url] = queue.Queue(), queue.Queue()
        return _INPROC_QUEUES[url]


# ---------------------------------------------------------------------------
# IPC helpers  (Unix domain sockets — non-Windows only)
# ---------------------------------------------------------------------------

def _parse_ipc_url(url: str) -> str:
    """``"ipc:///tmp/foo"`` → ``"/tmp/foo"``."""
    return url[len("ipc://"):]


async def _ipc_serve(path: str, ready, stop: threading.Event) -> None:  # type: ignore[type-arg]
    """Asyncio Unix-domain socket echo server coroutine."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(4)
                size = int.from_bytes(header, "big")
                await reader.readexactly(size)
                writer.write(b"\x00")
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    # Remove a stale socket file left by a previous run.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    server = await asyncio.start_unix_server(_handle, path)  # type: ignore[attr-defined]
    async with server:
        await server.start_serving()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()

    # Clean up the socket file after shutdown.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


async def _ipc_latency(
    path: str, msg_size: int, n_warmup: int, n_iters: int
) -> list[float]:
    """Run a latency benchmark over a Unix domain socket."""
    reader, writer = await asyncio.open_unix_connection(path)  # type: ignore[attr-defined]
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    samples: list[float] = []
    try:
        for i in range(n_warmup + n_iters):
            t0 = perf_counter()
            writer.write(payload)
            await writer.drain()
            await reader.readexactly(1)
            t1 = perf_counter()
            if i >= n_warmup:
                samples.append((t1 - t0) * 1e6)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return samples


async def _ipc_ops(path: str, msg_size: int, duration_s: float) -> float:
    """Run an ops/sec benchmark over a Unix domain socket."""
    reader, writer = await asyncio.open_unix_connection(path)  # type: ignore[attr-defined]
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    count = 0
    t_end = perf_counter() + duration_s
    try:
        while perf_counter() < t_end:
            writer.write(payload)
            await writer.drain()
            await reader.readexactly(1)
            count += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return count / duration_s


# ---------------------------------------------------------------------------
# TCP helpers
# ---------------------------------------------------------------------------

def _parse_tcp_url(url: str) -> tuple[str, int]:
    """``"tcp://127.0.0.1:54321"`` → ``("127.0.0.1", 54321)``."""
    # Strip the scheme
    rest = url[len("tcp://"):]
    host, port_str = rest.rsplit(":", 1)
    return host, int(port_str)


async def _tcp_serve(host: str, port: int, ready, stop: threading.Event | None) -> None:
    """Asyncio TCP echo server coroutine."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(4)
                size = int.from_bytes(header, "big")
                await reader.readexactly(size)
                writer.write(b"\x00")
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, host, port)
    async with server:
        await server.start_serving()
        ready.set()
        # Poll until the (thread) stop event is set.
        while not stop.is_set():
            await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()


async def _tcp_latency(
    host: str, port: int, msg_size: int, n_warmup: int, n_iters: int
) -> list[float]:
    reader, writer = await asyncio.open_connection(host, port)
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    samples: list[float] = []
    try:
        for i in range(n_warmup + n_iters):
            t0 = perf_counter()
            writer.write(payload)
            await writer.drain()
            await reader.readexactly(1)
            t1 = perf_counter()
            if i >= n_warmup:
                samples.append((t1 - t0) * 1e6)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return samples


async def _tcp_ops(host: str, port: int, msg_size: int, duration_s: float) -> float:
    reader, writer = await asyncio.open_connection(host, port)
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    count = 0
    t_end = perf_counter() + duration_s
    try:
        while perf_counter() < t_end:
            writer.write(payload)
            await writer.drain()
            await reader.readexactly(1)
            count += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return count / duration_s


# ---------------------------------------------------------------------------
# Competitor class
# ---------------------------------------------------------------------------

class PythonRawAsyncBenchmark(BaseBenchmark):
    """Baseline: raw Python inproc (queue.Queue) and TCP (asyncio streams)."""

    name = "python_raw_async"

    # ---- Server ------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        if url.startswith("inproc://"):
            req_q, resp_q = _get_inproc_queues(url)
            ready.set()
            while not stop.is_set():
                try:
                    msg = req_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                resp_q.put(b"")
            with _INPROC_LOCK:
                _INPROC_QUEUES.pop(url, None)

        elif url.startswith("ipc://"):
            if not _IPC_SUPPORTED:
                # Not supported on Windows — signal ready and idle.
                ready.set()
                stop.wait()
                return
            path = _parse_ipc_url(url)
            asyncio.run(_ipc_serve(path, ready, stop))

        elif url.startswith("tcp://"):
            host, port = _parse_tcp_url(url)
            asyncio.run(_tcp_serve(host, port, ready, stop))

        else:
            raise ValueError(f"Unsupported URL scheme: {url!r}")

    # ---- Latency -----------------------------------------------------------

    def measure_latency(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        if transport_url.startswith("ipc://"):
            if not _IPC_SUPPORTED:
                raise NotImplementedError("ipc not supported for python_raw_async on Windows")
            path = _parse_ipc_url(transport_url)
            return asyncio.run(_ipc_latency(path, msg_size, n_warmup, n_iters))

        if transport_url.startswith("inproc://"):
            req_q, resp_q = _get_inproc_queues(transport_url)
            data = b"\x00" * msg_size
            samples: list[float] = []
            for i in range(n_warmup + n_iters):
                t0 = perf_counter()
                req_q.put(data)
                resp_q.get()
                t1 = perf_counter()
                if i >= n_warmup:
                    samples.append((t1 - t0) * 1e6)
            return samples

        if transport_url.startswith("tcp://"):
            host, port = _parse_tcp_url(transport_url)
            return asyncio.run(_tcp_latency(host, port, msg_size, n_warmup, n_iters))

        raise ValueError(f"Unsupported URL scheme: {transport_url!r}")

    # ---- Ops/sec -----------------------------------------------------------

    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        if transport_url.startswith("ipc://"):
            if not _IPC_SUPPORTED:
                raise NotImplementedError("ipc not supported for python_raw_async on Windows")
            path = _parse_ipc_url(transport_url)
            return asyncio.run(_ipc_ops(path, msg_size, duration_s))

        if transport_url.startswith("inproc://"):
            req_q, resp_q = _get_inproc_queues(transport_url)
            data = b"\x00" * msg_size
            count = 0
            t_start = perf_counter()
            t_end = t_start + duration_s
            while perf_counter() < t_end:
                req_q.put(data)
                resp_q.get()
                count += 1
            elapsed = perf_counter() - t_start
            return count / elapsed

        if transport_url.startswith("tcp://"):
            host, port = _parse_tcp_url(transport_url)
            return asyncio.run(_tcp_ops(host, port, msg_size, duration_s))

        raise ValueError(f"Unsupported URL scheme: {transport_url!r}")


COMPETITORS["python_raw_async"] = PythonRawAsyncBenchmark

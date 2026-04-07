"""python_raw2 — raw Python baseline competitor using blocking socket APIs.

Like :mod:`python_raw` but replaces asyncio with synchronous, blocking socket
calls for the ipc and tcp transports:

* **inproc** — same as python_raw: a pair of :class:`queue.Queue` objects.

* **ipc** — Unix-domain stream sockets via :mod:`socket` (``AF_UNIX``).
  Server runs a blocking ``accept`` loop in a thread.
  **Not supported on Windows** — methods raise :exc:`NotImplementedError`.

* **tcp** — Plain TCP sockets via :mod:`socket` (``AF_INET``).
  Server runs a blocking ``accept`` loop in a thread.

Both server and client run in separate threads (orchestrated by the runner
as for any other non-inproc competitor).

Wire framing (ipc & tcp)
------------------------
4-byte big-endian length prefix followed by *msg_size* payload bytes.
Server echoes back a single ``b"\\x00"`` byte per message.
"""

import os
import queue
import socket
import sys
import threading
from time import perf_counter

from .base import BaseBenchmark
from .._core.common import COMPETITORS

# IPC (Unix domain sockets) is not available on Windows.
_IPC_SUPPORTED: bool = sys.platform != "win32"

# ---------------------------------------------------------------------------
# Shared inproc state  (identical to python_raw)
# ---------------------------------------------------------------------------

_INPROC_QUEUES: dict[str, tuple[queue.Queue, queue.Queue]] = {}
_INPROC_LOCK = threading.Lock()


def _get_inproc_queues(url: str) -> tuple[queue.Queue, queue.Queue]:
    with _INPROC_LOCK:
        if url not in _INPROC_QUEUES:
            _INPROC_QUEUES[url] = queue.Queue(), queue.Queue()
        return _INPROC_QUEUES[url]


# ---------------------------------------------------------------------------
# Low-level socket helpers
# ---------------------------------------------------------------------------

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*; raise :exc:`ConnectionResetError` on EOF."""
    buf = bytearray(n)
    view = memoryview(buf)
    received = 0
    while received < n:
        chunk = sock.recv_into(view[received:], n - received)
        if chunk == 0:
            raise ConnectionResetError("connection closed")
        received += chunk
    return bytes(buf)


def _handle_conn(conn: socket.socket) -> None:
    """Echo server handler: read framed messages, reply with a single byte."""
    with conn:
        try:
            while True:
                header = _recv_exactly(conn, 4)
                size = int.from_bytes(header, "big")
                _recv_exactly(conn, size)
                conn.sendall(b"\x00")
        except (ConnectionResetError, OSError):
            pass


def _accept_loop(
    server_sock: socket.socket,
    ready,
    stop: threading.Event,
) -> None:
    """Accept connections in a loop until *stop* is set."""
    server_sock.settimeout(0.2)
    ready.set()
    try:
        while not stop.is_set():
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                continue
            # Handle each connection in its own daemon thread so the accept
            # loop is never blocked behind a single client.
            t = threading.Thread(target=_handle_conn, args=(conn,), daemon=True)
            t.start()
    finally:
        server_sock.close()


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------

def _parse_ipc_url(url: str) -> str:
    return url[len("ipc://"):]


def _ipc_serve(path: str, ready, stop: threading.Event) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(path)
    server_sock.listen(16)
    try:
        _accept_loop(server_sock, ready, stop)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _ipc_latency(path: str, msg_size: int, n_warmup: int, n_iters: int) -> list[float]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    samples: list[float] = []
    try:
        for i in range(n_warmup + n_iters):
            t0 = perf_counter()
            sock.sendall(payload)
            _recv_exactly(sock, 1)
            t1 = perf_counter()
            if i >= n_warmup:
                samples.append((t1 - t0) * 1e6)
    finally:
        sock.close()
    return samples


def _ipc_ops(path: str, msg_size: int, duration_s: float) -> float:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    count = 0
    t_end = perf_counter() + duration_s
    try:
        while perf_counter() < t_end:
            sock.sendall(payload)
            _recv_exactly(sock, 1)
            count += 1
    finally:
        sock.close()
    elapsed = perf_counter() - (t_end - duration_s)
    return count / duration_s


# ---------------------------------------------------------------------------
# TCP helpers
# ---------------------------------------------------------------------------

def _parse_tcp_url(url: str) -> tuple[str, int]:
    rest = url[len("tcp://"):]
    host, port_str = rest.rsplit(":", 1)
    return host, int(port_str)


def _tcp_serve(host: str, port: int, ready, stop: threading.Event) -> None:
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(16)
    _accept_loop(server_sock, ready, stop)


def _tcp_latency(host: str, port: int, msg_size: int, n_warmup: int, n_iters: int) -> list[float]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    samples: list[float] = []
    try:
        for i in range(n_warmup + n_iters):
            t0 = perf_counter()
            sock.sendall(payload)
            _recv_exactly(sock, 1)
            t1 = perf_counter()
            if i >= n_warmup:
                samples.append((t1 - t0) * 1e6)
    finally:
        sock.close()
    return samples


def _tcp_ops(host: str, port: int, msg_size: int, duration_s: float) -> float:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    payload = msg_size.to_bytes(4, "big") + b"\x00" * msg_size
    count = 0
    t_end = perf_counter() + duration_s
    try:
        while perf_counter() < t_end:
            sock.sendall(payload)
            _recv_exactly(sock, 1)
            count += 1
    finally:
        sock.close()
    return count / duration_s


# ---------------------------------------------------------------------------
# Competitor class
# ---------------------------------------------------------------------------

class PythonRaw2Benchmark(BaseBenchmark):
    """Baseline: raw Python inproc (queue.Queue) and blocking socket streams."""

    name = "python_raw2"

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
                ready.set()
                stop.wait()
                return
            path = _parse_ipc_url(url)
            _ipc_serve(path, ready, stop)

        elif url.startswith("tcp://"):
            host, port = _parse_tcp_url(url)
            _tcp_serve(host, port, ready, stop)

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
                raise NotImplementedError("ipc not supported for python_raw2 on Windows")
            path = _parse_ipc_url(transport_url)
            return _ipc_latency(path, msg_size, n_warmup, n_iters)

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
            return _tcp_latency(host, port, msg_size, n_warmup, n_iters)

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
                raise NotImplementedError("ipc not supported for python_raw2 on Windows")
            path = _parse_ipc_url(transport_url)
            return _ipc_ops(path, msg_size, duration_s)

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
            return _tcp_ops(host, port, msg_size, duration_s)

        raise ValueError(f"Unsupported URL scheme: {transport_url!r}")


COMPETITORS["python_raw2"] = PythonRaw2Benchmark

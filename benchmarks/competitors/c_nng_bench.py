"""Benchmark competitor: C nng binaries (bench_server + bench_client_*)."""

from __future__ import annotations

import json
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from .base import BaseBenchmark
from .._core.common import COMPETITORS, C_NNG_DIR, C_BUILD_DIR
from .._core.stats import Stats, compute_stats_from_c_json


class _Bins(NamedTuple):
    """Paths to the compiled C benchmark binaries."""

    server: Path
    client_lat: Path
    client_ops: Path
    inproc_lat: Path
    inproc_ops: Path


# On Windows executables have a .exe suffix; on POSIX there is no suffix.
_EXE: str = ".exe" if sys.platform == "win32" else ""


def _ensure_built() -> _Bins:
    """Build the C nng benchmarks if not already built and return binary paths."""
    bins = _Bins(
        server     = C_BUILD_DIR / f"bench_server{_EXE}",
        client_lat = C_BUILD_DIR / f"bench_client_lat{_EXE}",
        client_ops = C_BUILD_DIR / f"bench_client_ops{_EXE}",
        inproc_lat = C_BUILD_DIR / f"bench_inproc_lat{_EXE}",
        inproc_ops = C_BUILD_DIR / f"bench_inproc_ops{_EXE}",
    )

    if all(p.exists() for p in bins):
        return bins

    print("  [c_nng] Building C nng benchmarks …")
    C_BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # Use modern -S/-B flags so source and build directories are always explicit.
    # CMAKE_RUNTIME_OUTPUT_DIRECTORY ensures binaries land in C_BUILD_DIR regardless
    # of the platform default (avoids Debug/Release subdirectory surprises on Windows).
    subprocess.run(
        [
            "cmake",
            "-S", str(C_NNG_DIR),
            "-B", str(C_BUILD_DIR),
            "-DCMAKE_BUILD_TYPE=Release",
            # Single-config generators (Makefile/Ninja) use CMAKE_RUNTIME_OUTPUT_DIRECTORY.
            # Multi-config generators (Visual Studio) require the per-config override.
            f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={C_BUILD_DIR}",
            f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE={C_BUILD_DIR}",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(C_BUILD_DIR), "--config", "Release", "--parallel"],
        check=True,
    )

    missing = [p.name for p in bins if not p.exists()]
    if missing:
        raise RuntimeError(
            f"C nng build succeeded but binaries not found in {C_BUILD_DIR}: {missing}"
        )

    print("  [c_nng] Build complete.")
    return bins


def _wait_for_ready(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Wait until the server prints 'READY' on stdout.

    Reads stdout in a daemon thread feeding a Queue so that the main thread
    can apply a deadline without blocking on readline().  This also works on
    Windows where select() cannot be used with subprocess pipe handles.
    """
    line_q: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line_q.put(line)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            line = line_q.get(timeout=min(0.2, remaining))
            if line.strip() == "READY":
                return
        except queue.Empty:
            pass
        if proc.poll() is not None:
            stderr_out = proc.stderr.read().strip() if proc.stderr else ""
            raise RuntimeError(
                f"bench_server exited (code {proc.returncode}) before printing READY."
                + (f"\nstderr: {stderr_out}" if stderr_out else "")
            )
    stderr_out = proc.stderr.read().strip() if proc.stderr else ""
    raise TimeoutError(
        f"bench_server did not print READY within {timeout}s."
        + (f"\nstderr: {stderr_out}" if stderr_out else "")
    )


def _run_binary(bin_path: Path, args: list[str], *, timeout: int = 120) -> dict:
    """Run a self-contained benchmark binary and return parsed JSON stdout."""
    result = subprocess.run(
        [str(bin_path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{bin_path.name} failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return json.loads(result.stdout)


def _run_via_server(
    transport_url: str, client_bin: Path, client_args: list[str], *, timeout: int = 120
) -> dict:
    """Start bench_server at transport_url, run client_bin, and return parsed JSON."""
    server_bin = _ensure_built().server
    server = subprocess.Popen(
        [str(server_bin), transport_url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_ready(server)
        return _run_binary(client_bin, [transport_url, *client_args], timeout=timeout)
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


class CNngBenchmark(BaseBenchmark):
    """Measures pure C nng (no Python overhead) by running the bench_* binaries."""

    name = "c_nng"

    def _run_lat(self, transport_url: str, msg_size: int, n_warmup: int, n_iters: int) -> dict:
        """Run a latency measurement and return the raw JSON dict."""
        bins = _ensure_built()
        args = [str(msg_size), str(n_warmup), str(n_iters)]
        if transport_url.startswith("inproc://"):
            return _run_binary(bins.inproc_lat, [transport_url, *args])
        return _run_via_server(transport_url, bins.client_lat, args)

    def _run_ops(self, transport_url: str, msg_size: int, n_warmup: int, duration_s: float) -> dict:
        """Run an ops/sec measurement and return the raw JSON dict."""
        bins = _ensure_built()
        args = [str(msg_size), str(n_warmup), str(duration_s)]
        if transport_url.startswith("inproc://"):
            return _run_binary(bins.inproc_ops, [transport_url, *args])
        return _run_via_server(transport_url, bins.client_ops, args)

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
        """C client records per-RTT times internally; we return the distribution
        reconstructed from the five-number summary.

        Note: because the C binary only outputs summary stats (not raw samples),
        we synthesise a representative sample array that exactly matches the
        reported statistics.  The runner stores the Stats object directly for C.
        """
        d = self._run_lat(transport_url, msg_size, n_warmup, n_iters)
        return [d["min_us"], d["p50_us"], d["p95_us"], d["p99_us"], d["max_us"]]

    def measure_latency_stats(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> Stats:
        """Return Stats directly (more accurate than going via sample list)."""
        return compute_stats_from_c_json(self._run_lat(transport_url, msg_size, n_warmup, n_iters))

    # ------------------------------------------------------------------
    # Ops/sec
    # ------------------------------------------------------------------

    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        """Return achieved ops/sec over *duration_s* seconds."""
        d = self._run_ops(transport_url, msg_size, 200, duration_s)
        return float(d.get("ops_per_sec", 0.0))


COMPETITORS["c_nng"] = CNngBenchmark

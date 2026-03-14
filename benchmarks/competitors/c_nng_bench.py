"""Benchmark competitor: C nng binaries (bench_server + bench_client)."""

from __future__ import annotations

import json
import select
import signal
import subprocess
import time
from pathlib import Path

from .base import BaseBenchmark
from .._core.common import COMPETITORS, C_NNG_DIR, C_BUILD_DIR
from .._core.stats import Stats, compute_stats_from_c_json


def _ensure_built() -> tuple[Path, Path]:
    """Build the C nng benchmarks if not already built.  Returns (server_bin, client_bin)."""
    server_bin = C_BUILD_DIR / "bench_server"
    client_bin = C_BUILD_DIR / "bench_client"

    if server_bin.exists() and client_bin.exists():
        return server_bin, client_bin

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
            f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={C_BUILD_DIR}",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(C_BUILD_DIR), "--parallel"],
        check=True,
    )

    if not server_bin.exists() or not client_bin.exists():
        raise RuntimeError(
            f"C nng build succeeded but binaries not found in {C_BUILD_DIR}"
        )

    print("  [c_nng] Build complete.")
    return server_bin, client_bin


def _wait_for_ready(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Wait until the server prints 'READY' on stdout.

    Uses select() to avoid blocking the deadline loop on readline().
    Surfaces stderr in the error message when the server exits unexpectedly.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        # select() lets us honour the deadline without blocking on readline()
        ready, _, _ = select.select([proc.stdout], [], [], min(0.2, remaining))
        if ready:
            line = proc.stdout.readline()
            if line.strip() == "READY":
                return
            if not line:  # EOF — process already exited
                break
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


class CNngBenchmark(BaseBenchmark):
    """Measures pure C nng (no Python overhead) by running bench_server and bench_client."""

    name = "c_nng"

    def _run_pair(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> dict:
        """Start server, run client, return parsed JSON stats dict.

        Raises NotImplementedError for inproc:// URLs because that transport
        requires both endpoints to share the same process address space.
        """
        if transport_url.startswith("inproc://"):
            raise NotImplementedError(
                "c_nng runs as separate processes — inproc:// is not supported"
            )
        server_bin, client_bin = _ensure_built()

        server = subprocess.Popen(
            [str(server_bin), transport_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_ready(server)

            result = subprocess.run(
                [
                    str(client_bin),
                    transport_url,
                    str(msg_size),
                    str(n_warmup),
                    str(n_iters),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"bench_client failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
                )

            return json.loads(result.stdout)
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()

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
        d = self._run_pair(transport_url, msg_size, n_warmup, n_iters)
        # Return a minimal list that encodes the stats for compute_stats to roundtrip.
        # In practice the runner calls _run_pair_stats directly.
        return [d["min_us"], d["p50_us"], d["p95_us"], d["p99_us"], d["max_us"]]

    def measure_latency_stats(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> Stats:
        """Return Stats directly (more accurate than going via sample list)."""
        d = self._run_pair(transport_url, msg_size, n_warmup, n_iters)
        return compute_stats_from_c_json(d)

    # ------------------------------------------------------------------
    # Bandwidth
    # ------------------------------------------------------------------

    def measure_bandwidth(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        d = self._run_pair(transport_url, msg_size, n_warmup, n_iters)
        # Derive bandwidth from RTT stats: bw = 2 * msg_size / rtt_s  (MB/s)
        def rtt_to_bw(rtt_us: float) -> float:
            return 2 * msg_size / (rtt_us / 1e6) / 1e6 if rtt_us > 0 else 0.0

        # Note: min_rtt → max_bw, max_rtt → min_bw
        return [
            rtt_to_bw(d["max_us"]),   # min bw (from max rtt)
            rtt_to_bw(d["p99_us"]),
            rtt_to_bw(d["p50_us"]),
            rtt_to_bw(d["p95_us"]),   # note: non-monotonic mapping, keep raw
            rtt_to_bw(d["min_us"]),   # max bw (from min rtt)
        ]

    def measure_bandwidth_stats(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> Stats:
        d = self._run_pair(transport_url, msg_size, n_warmup, n_iters)
        mean_bw = 2 * msg_size / (d["mean_us"] / 1e6) / 1e6 if d["mean_us"] > 0 else 0.0
        return Stats(
            min=2 * msg_size / (d["max_us"] / 1e6) / 1e6,
            p50=2 * msg_size / (d["p50_us"] / 1e6) / 1e6,
            p95=2 * msg_size / (d["p95_us"] / 1e6) / 1e6,
            p99=2 * msg_size / (d["p99_us"] / 1e6) / 1e6,
            max=2 * msg_size / (d["min_us"] / 1e6) / 1e6 if d["min_us"] > 0 else 0.0,
            mean=mean_bw,
            std=0.0,
            n=d.get("n", 0),
        )

    # ------------------------------------------------------------------
    # Ops/sec
    # ------------------------------------------------------------------

    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        # Estimate n_iters from expected throughput with a generous safety factor.
        n_iters = max(1000, int(duration_s * 100_000))
        d = self._run_pair(transport_url, msg_size, 200, n_iters)
        return float(d.get("ops_per_sec", 0.0))


COMPETITORS["c_nng"] = CNngBenchmark

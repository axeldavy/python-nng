"""Central benchmark runner.

Drives all registered competitors across all requested metrics, transports,
and message sizes, and collects results into a nested dict:

    results[metric][transport][msg_size][competitor] = Stats

Where *metric* is one of ``"latency"``, ``"bandwidth"``, ``"ops"``.
"""

from __future__ import annotations

import importlib
import json
import traceback
from pathlib import Path
from typing import Any

from ._core.common import COMPETITORS, MSG_SIZES, TRANSPORTS, next_url, RESULTS_DIR
from ._core.stats import Stats, compute_stats

# Import all competitor modules so they register themselves in COMPETITORS.
_COMPETITOR_MODULES = [
    "benchmarks.competitors.nng_sync",
    "benchmarks.competitors.nng_async",
    "benchmarks.competitors.zmq_sync",
    "benchmarks.competitors.zmq_async",
    "benchmarks.competitors.pynng_sync",
    "benchmarks.competitors.pynng_async",
    "benchmarks.competitors.c_nng_bench",
]

Results = dict[str, dict[str, dict[int, dict[str, Stats]]]]


def _import_competitors(requested: list[str]) -> None:
    """Import each competitor module.  Missing optional deps emit a warning."""
    for mod_path in _COMPETITOR_MODULES:
        comp_name = mod_path.rsplit(".", 1)[-1]
        # Map module suffix → competitor name
        name_map = {
            "nng_sync": "nng_sync",
            "nng_async": "nng_async",
            "zmq_sync": "zmq_sync",
            "zmq_async": "zmq_async",
            "pynng_sync": "pynng_sync",
            "pynng_async": "pynng_async",
            "c_nng_bench": "c_nng",
        }
        friendly = name_map.get(comp_name, comp_name)
        if requested and friendly not in requested:
            continue
        try:
            importlib.import_module(mod_path)
        except ImportError as exc:
            print(f"  [warn] Could not import {mod_path}: {exc}")


def _make_results() -> Results:
    return {"latency": {}, "bandwidth": {}, "ops": {}}


def _ensure_nested(results: Results, metric: str, transport: str, msg_size: int) -> None:
    results[metric].setdefault(transport, {}).setdefault(msg_size, {})


def run(
    competitors: list[str] | None = None,
    transports: list[str] | None = None,
    msg_sizes: list[int] | None = None,
    n_warmup: int = 500,
    n_iters: int = 3_000,
    duration_s: float = 5.0,
    verbose: bool = True,
) -> Results:
    """Run all requested benchmarks and return the results dict.

    Parameters
    ----------
    competitors:
        Names of competitors to include (default: all registered).
    transports:
        Transport names to test (default: all in TRANSPORTS).
    msg_sizes:
        Message sizes in bytes (default: MSG_SIZES).
    n_warmup:
        Warmup iterations (discarded).
    n_iters:
        Measured iterations for latency and bandwidth.
    duration_s:
        Duration in seconds for ops/sec measurement.
    verbose:
        Print progress lines.
    """
    _import_competitors(competitors or [])

    active_competitors = competitors or list(COMPETITORS.keys())
    active_transports = transports or list(TRANSPORTS.keys())
    active_sizes = msg_sizes or MSG_SIZES

    results = _make_results()

    for transport in active_transports:
        for msg_size in active_sizes:
            for comp_name in active_competitors:
                if comp_name not in COMPETITORS:
                    print(f"  [warn] Unknown competitor: {comp_name!r} — skipping")
                    continue

                bench = COMPETITORS[comp_name]()
                url = next_url(transport)

                if verbose:
                    print(
                        f"  {comp_name:20s} | {transport:6s} | {msg_size:>9,} B | latency …",
                        end="", flush=True,
                    )
                try:
                    # --- Latency ---
                    if hasattr(bench, "measure_latency_stats"):
                        lat_stats = bench.measure_latency_stats(url, msg_size, n_warmup, n_iters)
                    else:
                        samples = bench.measure_latency(url, msg_size, n_warmup, n_iters)
                        lat_stats = compute_stats(samples)

                    _ensure_nested(results, "latency", transport, msg_size)
                    results["latency"][transport][msg_size][comp_name] = lat_stats

                    if verbose:
                        print(f" p50={lat_stats.p50:.1f}µs | bandwidth …", end="", flush=True)

                    # Use a fresh URL for bandwidth to avoid port reuse issues
                    url_bw = next_url(transport)

                    # --- Bandwidth ---
                    if hasattr(bench, "measure_bandwidth_stats"):
                        bw_stats = bench.measure_bandwidth_stats(url_bw, msg_size, n_warmup, n_iters)
                    else:
                        bw_samples = bench.measure_bandwidth(url_bw, msg_size, n_warmup, n_iters)
                        bw_stats = compute_stats(bw_samples)

                    _ensure_nested(results, "bandwidth", transport, msg_size)
                    results["bandwidth"][transport][msg_size][comp_name] = bw_stats

                    if verbose:
                        print(f" p50={bw_stats.p50:.2f}MB/s | ops …", end="", flush=True)

                    # Use a fresh URL for ops
                    url_ops = next_url(transport)

                    # --- Ops/sec ---
                    ops_mean = bench.measure_ops(url_ops, msg_size, duration_s)
                    ops_stats = Stats(
                        min=ops_mean, p50=ops_mean, p95=ops_mean,
                        p99=ops_mean, max=ops_mean, mean=ops_mean,
                        std=0.0, n=1,
                    )
                    _ensure_nested(results, "ops", transport, msg_size)
                    results["ops"][transport][msg_size][comp_name] = ops_stats

                    if verbose:
                        print(f" {ops_mean:,.0f} ops/s  ✓")

                except NotImplementedError as exc:
                    if verbose:
                        print(f" skipped ({exc})")
                except Exception:
                    if verbose:
                        print(f" ERROR")
                    traceback.print_exc()

    return results


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

def results_to_json(results: Results) -> dict:
    """Convert Results to a plain dict suitable for json.dumps."""
    out: dict = {}
    for metric, transports in results.items():
        out[metric] = {}
        for transport, sizes in transports.items():
            out[metric][transport] = {}
            for msg_size, comps in sizes.items():
                out[metric][transport][str(msg_size)] = {
                    comp: stats.to_dict() for comp, stats in comps.items()
                }
    return out


def json_to_results(data: dict) -> Results:
    """Inverse of :func:`results_to_json`."""
    results = _make_results()
    for metric, transports in data.items():
        for transport, sizes in transports.items():
            for msg_size_str, comps in sizes.items():
                msg_size = int(msg_size_str)
                results[metric].setdefault(transport, {})[msg_size] = {
                    comp: Stats.from_dict(s) for comp, s in comps.items()
                }
    return results


def save_results(results: Results, output_dir: Path | None = None) -> Path:
    """Serialise results to JSON and return the file path."""
    out_dir = output_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "results.json"
    path.write_text(json.dumps(results_to_json(results), indent=2), encoding="utf-8")
    print(f"  wrote {path}")
    return path


def load_results(path: Path) -> Results:
    """Load results from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return json_to_results(data)

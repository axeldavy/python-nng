"""Central benchmark runner.

Drives all registered competitors across all requested metrics, transports,
and message sizes, and collects results into a nested dict:

    results[metric][transport][msg_size][competitor] = Stats

Where *metric* is one of ``"latency"`` or ``"ops"``.

Server isolation strategy
-------------------------
* **inproc** — server runs in a :class:`threading.Thread`; client runs in a
  second thread (so neither occupies the runner's main thread).
* **ipc / tcp** — server runs in a :class:`multiprocessing.Process`; client
  runs directly in the main runner process.

Self-contained competitors (currently only ``c_nng``) launch their own server
processes internally and bypass this orchestration.
"""

from __future__ import annotations

import gc
import importlib
import json
import multiprocessing
import threading
import traceback
from pathlib import Path

from ._core.common import COMPETITORS, MSG_SIZES, TRANSPORTS, next_url, RESULTS_DIR
from ._core.stats import Stats, compute_stats

# Import all competitor modules so they register themselves in COMPETITORS.
_COMPETITOR_MODULES = [
    "benchmarks.competitors.nng_sync1",
    "benchmarks.competitors.nng_sync2",
    "benchmarks.competitors.nng_async1",
    "benchmarks.competitors.nng_async2",
    "benchmarks.competitors.nng_async3",
    "benchmarks.competitors.nng_async4",
    "benchmarks.competitors.nng_async5",
    "benchmarks.competitors.nng_async6",
    "benchmarks.competitors.zmq_sync",
    "benchmarks.competitors.zmq_async",
    "benchmarks.competitors.pynng_sync",
    "benchmarks.competitors.pynng_async",
    "benchmarks.competitors.c_nng_bench",
    "benchmarks.competitors.python_raw",
]

Results = dict[str, dict[str, dict[int, dict[str, Stats]]]]


def _import_competitors(requested: list[str]) -> None:
    """Import each competitor module.  Missing optional deps emit a warning."""
    for mod_path in _COMPETITOR_MODULES:
        comp_name = mod_path.rsplit(".", 1)[-1]
        # Map module suffix → competitor name
        name_map = {
            "nng_sync1": "nng_sync1",
            "nng_sync2": "nng_sync2",
            "nng_async1": "nng_async1",
            "nng_async2": "nng_async2",
            "nng_async3": "nng_async3",
            "nng_async4": "nng_async4",
            "nng_async5": "nng_async5",
            "nng_async6": "nng_async6",
            "zmq_sync": "zmq_sync",
            "zmq_async": "zmq_async",
            "pynng_sync": "pynng_sync",
            "pynng_async": "pynng_async",
            "c_nng_bench": "c_nng",
            "python_raw": "python_raw",
        }
        friendly = name_map.get(comp_name, comp_name)
        if requested and friendly not in requested:
            continue
        try:
            importlib.import_module(mod_path)
        except ImportError as exc:
            print(f"  [warn] Could not import {mod_path}: {exc}")


def _make_results() -> Results:
    return {"latency": {}, "ops": {}}


def _ensure_nested(results: Results, metric: str, transport: str, msg_size: int) -> None:
    results[metric].setdefault(transport, {}).setdefault(msg_size, {})


# ---------------------------------------------------------------------------
# Server orchestration helpers
# ---------------------------------------------------------------------------

def _make_events(transport: str):
    """Return (ready, stop) event objects suited for the transport's isolation mode."""
    if transport == "inproc":
        return threading.Event(), threading.Event()
    return multiprocessing.Event(), multiprocessing.Event()


def _launch_server(bench_cls, transport: str, url: str, ready, stop):
    """Start the benchmark server in a thread (inproc) or process (ipc/tcp)."""
    if transport == "inproc":
        worker = threading.Thread(
            target=bench_cls.run_server,
            args=(url, ready, stop),
            daemon=True,
        )
    else:
        worker = multiprocessing.Process(
            target=bench_cls.run_server,
            args=(url, ready, stop),
            daemon=True,
        )
    worker.start()
    return worker


def _stop_server(worker, stop_event) -> None:
    """Signal the server to stop and wait for it to exit."""
    stop_event.set()
    worker.join(timeout=5)
    if isinstance(worker, multiprocessing.Process) and worker.is_alive():
        worker.kill()
        worker.join(timeout=2)


def _run_in_thread(fn, *args):
    """Run *fn*\\(*args*) in a daemon thread; propagate any exception to the caller."""
    result_box: list = [None]
    exc_box: list = [None]

    def _wrapper() -> None:
        try:
            result_box[0] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            exc_box[0] = exc

    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    t.join()
    if exc_box[0] is not None:
        raise exc_box[0]
    return result_box[0]


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

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
        Measured iterations for latency.
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

                bench_cls = COMPETITORS[comp_name]
                bench = bench_cls()
                # c_nng launches its own C server subprocesses internally;
                # all other competitors expose run_server for the runner to use.
                is_self_contained = (comp_name == "c_nng")

                if verbose:
                    print(
                        f"  {comp_name:20s} | {transport:6s} | {msg_size:>9,} B | latency …",
                        end="", flush=True,
                    )
                try:
                    # ---- Latency ------------------------------------------------
                    url = next_url(transport)

                    if is_self_contained:
                        if hasattr(bench, "measure_latency_stats"):
                            lat_stats = bench.measure_latency_stats(
                                url, msg_size, n_warmup, n_iters
                            )
                        else:
                            lat_stats = compute_stats(
                                bench.measure_latency(url, msg_size, n_warmup, n_iters)
                            )
                    else:
                        ready, stop = _make_events(transport)
                        server = _launch_server(bench_cls, transport, url, ready, stop)
                        if not ready.wait(timeout=5):
                            _stop_server(server, stop)
                            raise RuntimeError("Server did not become ready within 5 s")
                        try:
                            if transport == "inproc":
                                samples = _run_in_thread(
                                    bench.measure_latency, url, msg_size, n_warmup, n_iters
                                )
                            else:
                                samples = bench.measure_latency(
                                    url, msg_size, n_warmup, n_iters
                                )
                        finally:
                            _stop_server(server, stop)
                        lat_stats = compute_stats(samples)

                    _ensure_nested(results, "latency", transport, msg_size)
                    results["latency"][transport][msg_size][comp_name] = lat_stats

                    if verbose:
                        print(f" p50={lat_stats.p50:.1f}µs | ops …", end="", flush=True)

                    # ---- Ops/sec ------------------------------------------------
                    url_ops = next_url(transport)

                    if is_self_contained:
                        ops_mean = bench.measure_ops(url_ops, msg_size, duration_s)
                    else:
                        ready_ops, stop_ops = _make_events(transport)
                        server_ops = _launch_server(
                            bench_cls, transport, url_ops, ready_ops, stop_ops
                        )
                        if not ready_ops.wait(timeout=5):
                            _stop_server(server_ops, stop_ops)
                            raise RuntimeError("Server (ops) did not become ready within 5 s")
                        try:
                            if transport == "inproc":
                                ops_mean = _run_in_thread(
                                    bench.measure_ops, url_ops, msg_size, duration_s
                                )
                            else:
                                ops_mean = bench.measure_ops(url_ops, msg_size, duration_s)
                        finally:
                            _stop_server(server_ops, stop_ops)

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
                        print(" ERROR")
                    traceback.print_exc()
                finally:
                    del bench
                    gc.collect()

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
        if metric not in results:
            continue  # ignore stale keys (e.g. "bandwidth" from old result files)
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


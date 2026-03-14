#!/usr/bin/env python3
"""
run_benchmarks.py — Top-level CLI for the python-nng benchmark suite.

Run from the workspace root:

    python -m benchmarks.run_benchmarks [OPTIONS]

Or:

    python benchmarks/run_benchmarks.py [OPTIONS]

Examples
--------
Smoke-run (fast):
    python -m benchmarks.run_benchmarks \\
        --competitors nng_sync \\
        --transports inproc \\
        --sizes 64 \\
        --iters 100 --warmup 10 --no-plot

Full run:
    python -m benchmarks.run_benchmarks

Reload a previous result and regenerate plots:
    python -m benchmarks.run_benchmarks --load results/results.json \\
        --no-run --plot --markdown
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the workspace root is on sys.path so `benchmarks.*` imports work
# both when invoked as a module (-m) and as a plain script.
_WORKSPACE_ROOT = Path(__file__).parent.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

import json

import click

from benchmarks._core.common import MSG_SIZES, TRANSPORTS, RESULTS_DIR
from benchmarks.runner import run, save_results, load_results
from benchmarks.reporting import MarkdownReporter, PlotReporter

_ALL_COMPETITORS = [
    "c_nng",
    "nng_sync",
    "nng_async",
    "zmq_sync",
    "zmq_async",
    "pynng_sync",
    "pynng_async",
]
_ALL_TRANSPORTS = list(TRANSPORTS.keys())


@click.command(context_settings={"show_default": True})
@click.option(
    "--competitors", "-c",
    multiple=True,
    type=click.Choice(_ALL_COMPETITORS),
    default=_ALL_COMPETITORS,
    help="Which competitors to benchmark (repeat flag for multiple).",
)
@click.option(
    "--transports", "-t",
    multiple=True,
    type=click.Choice(_ALL_TRANSPORTS),
    default=_ALL_TRANSPORTS,
    help="Which transports to benchmark.",
)
@click.option(
    "--sizes", "-s",
    multiple=True,
    type=int,
    default=MSG_SIZES,
    help="Message sizes in bytes to sweep.",
)
@click.option("--iters",    default=3_000,  help="Iterations per measurement.")
@click.option("--warmup",   default=500,    help="Warmup iterations (discarded).")
@click.option("--duration", default=5.0,    help="Duration (s) for ops/sec measurement.")
@click.option("--output-dir", "-o",
              default=str(RESULTS_DIR),
              type=click.Path(),
              help="Directory for JSON, Markdown, and PNG outputs.")
@click.option("--run/--no-run",       "do_run",  default=True,  help="Execute benchmarks.")
@click.option("--markdown/--no-markdown",          default=True,  help="Write Markdown tables.")
@click.option("--plot/--no-plot",                  default=True,  help="Write PNG plots.")
@click.option("--load",
              default=None,
              type=click.Path(exists=True),
              help="Load previous results.json instead of running benchmarks.")
@click.option("--verbose/--quiet", default=True)
def main(
    competitors: tuple[str, ...],
    transports: tuple[str, ...],
    sizes: tuple[int, ...],
    iters: int,
    warmup: int,
    duration: float,
    output_dir: str,
    do_run: bool,
    markdown: bool,
    plot: bool,
    load: str | None,
    verbose: bool,
) -> None:
    out_dir = Path(output_dir)

    # ------------------------------------------------------------------ Load
    if load:
        if verbose:
            print(f"Loading results from {load} …")
        results = load_results(Path(load))
        # Infer sizes and transports from loaded data
        active_sizes = sorted(
            {ms for metric in results.values() for tr in metric.values() for ms in tr.keys()}
        )
        active_transports = sorted(
            {tr for metric in results.values() for tr in metric.keys()}
        )
    elif do_run:
        active_sizes = list(sizes) if sizes else MSG_SIZES
        active_transports = list(transports) if transports else _ALL_TRANSPORTS
        active_competitors = list(competitors) if competitors else _ALL_COMPETITORS

        if verbose:
            print("=" * 70)
            print("python-nng benchmark suite")
            print("=" * 70)
            print(f"  competitors : {active_competitors}")
            print(f"  transports  : {active_transports}")
            print(f"  msg sizes   : {active_sizes}")
            print(f"  iterations  : warmup={warmup}, measured={iters}")
            print(f"  ops duration: {duration}s")
            print(f"  output      : {out_dir}")
            print("=" * 70)

        results = run(
            competitors=active_competitors,
            transports=active_transports,
            msg_sizes=active_sizes,
            n_warmup=warmup,
            n_iters=iters,
            duration_s=duration,
            verbose=verbose,
        )
        save_results(results, out_dir)
    else:
        click.echo("Neither --run nor --load specified.  Nothing to do.")
        return

    # --------------------------------------------------------------- Reports
    if markdown:
        if verbose:
            print("\nWriting Markdown tables …")
        MarkdownReporter.full_report(results, out_dir)

    if plot:
        if verbose:
            print("\nWriting plots …")
        PlotReporter.full_report(results, out_dir, active_sizes)

    if verbose:
        print("\nDone.")


if __name__ == "__main__":
    main()

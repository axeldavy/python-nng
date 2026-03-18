"""Shared constants and configuration for the benchmark suite."""

from __future__ import annotations

import os
from pathlib import Path
import sys

# ---------------------------------------------------------------------------
# Message sizes to sweep (bytes)
# ---------------------------------------------------------------------------
MSG_SIZES: list[int] = [8, 64, 512, 4_096, 65_536, 1_048_576]

# ---------------------------------------------------------------------------
# Iteration counts
# ---------------------------------------------------------------------------
DEFAULT_WARMUP: int = 500
DEFAULT_ITERS: int = 3_000
DEFAULT_DURATION_S: float = 5.0   # used for the ops/sec measurement

# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------
# Each value is a URL template; "{port}" is substituted at runtime so that
# concurrent benchmark runs don't collide.
TRANSPORTS: dict[str, str] = {
    "inproc": "inproc://bench_{port}",
    "ipc":    "ipc:///tmp/bench_{port}.ipc",
    "tcp":    "tcp://127.0.0.1:{port}",
}

# Base port – each benchmark run requests a fresh one via _next_port().
_BASE_PORT = 54000
_port_counter = _BASE_PORT


def next_url(transport: str) -> str:
    """Return a fresh, unique URL for *transport* by bumping an in-process counter."""
    global _port_counter
    _port_counter += 1
    template = TRANSPORTS[transport]
    return template.format(port=_port_counter)


# ---------------------------------------------------------------------------
# Competitor registry
# ---------------------------------------------------------------------------
# Populated lazily by each competitors/*.py module when it is imported.
COMPETITORS: dict[str, type] = {}  # name -> BaseBenchmark subclass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BENCHMARKS_DIR = Path(__file__).parent.parent
RESULTS_DIR = BENCHMARKS_DIR / "results"
C_NNG_DIR = BENCHMARKS_DIR / "competitors" / "c_nng"
C_BUILD_DIR = C_NNG_DIR / "build"

## Event loop
def get_new_event_loop():
    """Return a new asyncio event loop.  Uses uvloop if available."""
    try:
        import uvloop
        return uvloop.new_event_loop()
    except ImportError:
        import asyncio
        return asyncio.SelectorEventLoop()

def run_in_new_loop(coro):
    """Run the given coroutine in a new event loop, returning the result."""
    sys.setswitchinterval(1e-6) # Switch gil frequently to not penalize threading aspects of the benchmarks

    loop = get_new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
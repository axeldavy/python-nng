"""Statistical utilities for benchmark sample arrays."""

import math
from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Stats:
    """Immutable summary statistics for a list of latency / bandwidth samples."""

    __slots__ = ("min", "p50", "p95", "p99", "max", "mean", "std", "n")

    def __init__(
        self,
        *,
        min: float,
        p50: float,
        p95: float,
        p99: float,
        max: float,
        mean: float,
        std: float,
        n: int,
    ) -> None:
        self.min = min
        self.p50 = p50
        self.p95 = p95
        self.p99 = p99
        self.max = max
        self.mean = mean
        self.std = std
        self.n = n

    def to_dict(self) -> dict[str, float | int]:
        return {
            "min": self.min,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "max": self.max,
            "mean": self.mean,
            "std": self.std,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Stats":
        return cls(**d)


def compute_stats(samples: Sequence[float]) -> Stats:
    """Compute summary statistics from *samples*.

    Parameters
    ----------
    samples:
        Flat sequence of numeric measurements (µs for latency, MB/s for
        bandwidth, etc.).  Must be non-empty.

    Returns
    -------
    Stats
    """
    if not samples:
        raise ValueError("samples must not be empty")
    arr = np.asarray(samples, dtype=np.float64)
    return Stats(
        min=float(arr.min()),
        p50=float(np.percentile(arr, 50)),
        p95=float(np.percentile(arr, 95)),
        p99=float(np.percentile(arr, 99)),
        max=float(arr.max()),
        mean=float(arr.mean()),
        std=float(arr.std()),
        n=len(arr),
    )


def compute_stats_from_c_json(d: dict) -> Stats:
    """Build a :class:`Stats` from the JSON dict emitted by the C benchmark binary.

    The C binary outputs keys ``min_us``, ``p50_us``, etc.
    """
    return Stats(
        min=float(d["min_us"]),
        p50=float(d["p50_us"]),
        p95=float(d["p95_us"]),
        p99=float(d["p99_us"]),
        max=float(d["max_us"]),
        mean=float(d["mean_us"]),
        std=float(d.get("std_us", math.nan)),
        n=int(d.get("n", 0)),
    )

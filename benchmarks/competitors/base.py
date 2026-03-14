"""Abstract base class that every benchmark competitor must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseBenchmark(ABC):
    """Protocol-agnostic benchmark interface.

    Each subclass sets up its own server and client (in threads or async tasks)
    for the requested transport URL and measures the desired metric.

    All timing values are in **microseconds (µs)**.
    Bandwidth values are in **megabytes per second (MB/s)**.
    """

    #: Human-readable short name used in tables and plots.
    name: str = "unknown"

    # ------------------------------------------------------------------
    # Latency
    # ------------------------------------------------------------------

    @abstractmethod
    def measure_latency(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        """Return per-round-trip latency samples in µs.

        The implementation must:
        1. Start a server in a background thread / task.
        2. Perform *n_warmup* round-trips (discarded).
        3. Record *n_iters* round-trips and return the list of RTT durations.
        4. Cleanly shut down server and client.

        Each sample is a one-way RTT measured on the *client* side:
        ``(t_after_recv - t_before_send) * 1e6``.
        """

    # ------------------------------------------------------------------
    # Bandwidth
    # ------------------------------------------------------------------

    @abstractmethod
    def measure_bandwidth(
        self,
        transport_url: str,
        msg_size: int,
        n_warmup: int,
        n_iters: int,
    ) -> list[float]:
        """Return per-burst bandwidth samples in MB/s.

        The implementation sends *n_warmup + n_iters* messages and measures
        throughput per burst of messages (or per message for single-pair
        request-reply where we measure how many bytes per second flow).

        Bandwidth = (msg_size bytes) / (RTT seconds) converted to MB/s.
        For push/pull style, measure total bytes / total time per window.
        """

    # ------------------------------------------------------------------
    # Ops/sec
    # ------------------------------------------------------------------

    @abstractmethod
    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        """Return mean ops/sec over a sustained *duration_s* second window.

        Unlike *measure_latency*, the server and client run as fast as
        possible for the given duration and the total operation count is
        divided by elapsed wall-clock time.
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} name={self.name!r}>"

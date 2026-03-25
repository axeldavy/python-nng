"""Abstract base class that every benchmark competitor must implement."""

from abc import ABC, abstractmethod


class BaseBenchmark(ABC):
    """Protocol-agnostic benchmark interface.

    The runner is responsible for launching the server (in a thread for inproc,
    or in a separate process for ipc/tcp) via :meth:`run_server`, waiting for
    it to be ready, then calling :meth:`measure_latency` / :meth:`measure_ops`
    (client-only — no server spawning inside those methods).

    All timing values are in **microseconds (µs)**.
    """

    #: Human-readable short name used in tables and plots.
    name: str = "unknown"

    # ------------------------------------------------------------------
    # Server entry point (called by the runner in a thread or process)
    # ------------------------------------------------------------------

    @classmethod
    def run_server(cls, url: str, ready, stop) -> None:
        """Run the REP server synchronously until *stop* is set.

        *ready* and *stop* are either :class:`threading.Event` or
        :class:`multiprocessing.Event` objects — both share the same API.
        Subclasses must override this method.  The default raises
        :exc:`NotImplementedError` (for self-contained competitors like c_nng
        that manage their own server process internally).
        """
        raise NotImplementedError(f"{cls.__name__} does not implement run_server")

    # ------------------------------------------------------------------
    # Latency  (client-only — server is already running at transport_url)
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

        The server is already listening at *transport_url* when this is called.
        The implementation must:
        1. Connect a client socket.
        2. Perform *n_warmup* round-trips (discarded).
        3. Record *n_iters* round-trips and return the list of RTT durations.
        4. Close the client socket.

        Each sample is a round-trip time measured on the client side:
        ``(t_after_recv - t_before_send) * 1e6``.
        """

    # ------------------------------------------------------------------
    # Ops/sec  (client-only — server is already running at transport_url)
    # ------------------------------------------------------------------

    @abstractmethod
    def measure_ops(
        self,
        transport_url: str,
        msg_size: int,
        duration_s: float,
    ) -> float:
        """Return mean ops/sec over a sustained *duration_s* second window.

        The server is already listening at *transport_url* when this is called.
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} name={self.name!r}>"

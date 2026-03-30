"""Background survey responder helper for service discovery services.

Provides :class:`ServiceResponder`, which wraps a :class:`nng.RespondentSocket`
in a single background daemon thread running an asyncio event loop.  One asyncio
task per context handles surveys concurrently, keeping the main service thread
completely free.  Because the thread is a daemon, no explicit cleanup is needed
— it exits automatically when the process terminates.

Why multiple contexts?
    A bare ``RespondentSocket`` processes one survey at a time.  Each additional
    context opens an independent state machine on the same socket, allowing N
    surveys to be in-flight simultaneously.

Thread-safety:
    :class:`ServiceResponder` is thread-safe.  :meth:`start` must be called
    before :meth:`stop`.
"""

import _thread
import asyncio
import atexit
import sys
import threading

import nng

# ---------------------------------------------------------------------------
# ServiceResponder
# ---------------------------------------------------------------------------


class ServiceResponder:
    """Run a RespondentSocket in a single background thread using asyncio.

    One asyncio task per context handles surveys concurrently; the service's
    main thread remains completely unblocked.

    Two commands are supported:

    * ``IDENTITY`` — replies with the service's *identity* string by default;
      *can* be overridden in *handlers* to return richer information.
    * ``TERMINATE`` — raises :exc:`KeyboardInterrupt` in the host process's
      main thread via :func:`_thread.interrupt_main`.
    """

    __slots__ = {
        "_identity": "Service identity string prepended to all responses",
        "_num_contexts": "Number of parallel asyncio context tasks",
        "_socket": "The underlying RespondentSocket",
        "_thread": "Single background thread running the asyncio event loop",
    }

    _identity: str
    _num_contexts: int
    _socket: nng.RespondentSocket
    _thread: threading.Thread | None

    def __init__(
        self,
        url: str,
        identity: str,
        *,
        num_contexts: int = 3,
    ) -> None:
        """Create the responder (does not start background threads yet).

        The underlying ``RespondentSocket`` is created immediately and dials
        *url* in non-blocking mode; nng retries the connection in the
        background until the surveyor comes online.

        Args:
            url: Surveyor URL to connect to (e.g. ``"tcp://127.0.0.1:54400"``).
            identity: Human-readable service label prepended to every response.
            num_contexts: Number of parallel asyncio tasks, one per context.
                Increase this value to serve more concurrent surveys.

        Raises:
            nng.NngError: If the dialer cannot be created.
        """
        self._identity = identity
        self._num_contexts = num_contexts
        self._thread = None

        # Create socket; block=False means we dial in the background and
        # reconnect automatically whenever the surveyor restarts.
        # We force connection to reattempt every 100ms to speed up connection
        # when a surveyor starts.
        self._socket = nng.RespondentSocket()
        self._socket.add_dialer(url, reconnect_max_ms=100).start(block=False)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the single background thread that runs the asyncio event loop.

        Inside the loop, one asyncio task per context handles surveys
        concurrently without extra OS threads.

        Raises:
            nng.NngError: If a context cannot be opened.
        """
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"responder-{self._identity}",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.stop)  # ensure cleanup on normal process exit

    def stop(self) -> None:
        """Close the socket, unblocking all pending ``arecv()`` calls.

        Each context task receives :exc:`~nng.NngClosed`, exits its loop, the
        ``TaskGroup`` completes, and the background thread terminates.
        """
        try:
            self._socket.close()
        except nng.NngError:
            pass  # already closed — harmless

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _handle_identity(self) -> str:
        """Built-in IDENTITY handler: return the service's identity string."""
        return self._identity

    def _handle_terminate(self) -> str:
        """Built-in TERMINATE handler: interrupt the main thread and reply.

        Calls :func:`_thread.interrupt_main`, which raises
        :exc:`KeyboardInterrupt` in the process's main thread.
        """
        _thread.interrupt_main()
        return "terminating"

    def _run_loop(self) -> None:
        """Entry point for the background thread; runs until the socket closes."""
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        """Open one asyncio task per context and wait for all to finish."""
        async with asyncio.TaskGroup() as tg:
            for ctx_id in range(self._num_contexts):
                ctx = self._socket.open_context()
                tg.create_task(
                    self._context_task(ctx, ctx_id),
                    name=f"{self._identity}-ctx{ctx_id}",
                )

    async def _context_task(self, ctx: nng.Context, ctx_id: int) -> None:
        """Asyncio task: receive surveys on *ctx* and send replies.

        Awaits ``arecv()`` until the socket is closed, then exits cleanly.

        Args:
            ctx: The dedicated context for this task.
            ctx_id: Numeric label used in diagnostic messages.
        """
        try:
            while True:
                # Await the next survey or NngClosed which signals shutdown
                try:
                    msg = await ctx.arecv()
                except nng.NngClosed:
                    break
                except nng.NngError as exc:
                    print(
                        f"[{self._identity}] ctx{ctx_id} recv error: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

                pipe = msg.pipe
                if pipe is not None:
                    print(
                        f"<{pipe.get_self_addr()}> [{self._identity}] ctx{ctx_id} received survey: '{msg}' from <{pipe.get_peer_addr()}>",
                        flush=True,
                    )

                # Parse the request and produce a reply
                message_text = msg.to_bytes().decode()
                if message_text == "IDENTITY":
                    response = self._handle_identity()
                elif message_text == "TERMINATE":
                    response = self._handle_terminate()
                else:
                    continue # ignore unknown commands

                # Send — the context ensures the reply reaches the correct surveyor
                try:
                    await ctx.asend(response)
                except nng.NngClosed:
                    break
                except nng.NngError as exc:
                    print(
                        f"[{self._identity}] ctx{ctx_id} send error: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                for pipe in self._socket.pipes:
                    print(
                        f"<{pipe.get_self_addr()}> [{self._identity}] ctx{ctx_id} sent reply: '{response}' to <{pipe.get_peer_addr()}>",
                        flush=True,
                    )
        finally:
            try:
                ctx.close()
            except nng.NngError:
                pass


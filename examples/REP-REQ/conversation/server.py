"""
This module implements the server side of the REP/REQ pattern where
the server opens a conversation with each new client.

Rather than having independent requests, this pattern is designed for long-lived
conversations with each client, where messages may be follow-ups to previous messages
and the server needs to maintain state per client.

This code is a simplified version of the secure REP server implementation
proposed in the secure_subscriber example, without any of the security aspects.
Use only in secure networks.
"""

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable
import traceback
from typing import TypeAlias


import nng



_DispatchItem: TypeAlias = tuple[nng.Message, nng.Context, asyncio.Event]



class _RepConversationManager:
    """Per-client nng transport manager for :class:`RepServer`.

    Owns the inbox queue, current-context tracking, and the per-client task.

    Not thread-safe.
    """

    __slots__ = {
        "_conversation_cb": "The conversation callback to invoke.",
        "_current_ctx": "Context and reply event from the last arecv, cleared by asend.",
        "_inbox": "Single message queue for all phases (handshake and application).",
        "_pipe": "The nng Pipe identifying the remote client.",
        "_running": "Whether the manager task is still running (used to prevent dispatching after disconnect).",
    }

    _conversation_cb: Callable[["RepConversation"], Awaitable[None]]
    _current_ctx: tuple[nng.Context, asyncio.Event] | None
    _inbox: asyncio.Queue[_DispatchItem | None]
    _pipe: nng.Pipe
    _running: bool

    def __init__(self, pipe: nng.Pipe, conversation_cb: Callable[["RepConversation"], Awaitable[None]]) -> None:
        """Initialise the manager for *pipe*.

        Args:
            pipe: The nng pipe identifying the remote client.
            conversation_cb: The conversation callback to invoke.

        """
        self._conversation_cb = conversation_cb
        self._current_ctx = None
        self._inbox = asyncio.Queue(maxsize=2) # one message + one None sentinel for disconnect
        self._pipe = pipe

        # Accept messages, assumes _execute will be scheduled
        # immediately after construction and before any dispatch calls.
        self._running = True

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    async def _execute(self) -> None:
        """Invoke the conversation callback.
        """

        # Run the conversation callback.
        try:
            conv = RepConversation(self)
            await self._conversation_cb(conv)

        # Do not report normal disconnects as errors.
        except ConnectionResetError:
            pass

        # Clean up on exit or error
        finally:
            # Stop accepting messages
            self._running = False

            # Close the pipe in case of exception or completion
            self._pipe.close()

            # Release any context we have ownership
            if self._current_ctx is not None:
                _, evt = self._current_ctx
                self._current_ctx = None
                evt.set()
            try:
                while True:
                    item = self._inbox.get_nowait()
                    if item is not None:
                        _, _, evt = item
                        evt.set()
            except asyncio.QueueEmpty:
                pass
            self._inbox.shutdown()

    # ------------------------------------------------------------------
    # Dispatch interface (called by RepServer._recv_dispatch)
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        msg: nng.Message,
        ctx: nng.Context,
        reply_event: asyncio.Event,
    ) -> None:
        """Enqueue an inbound message for :meth:`_execute` to consume via :meth:`arecv`.

        Called outside _execute to pass ownership of the message and the
        dispatch slot to the manager. The manager is responsible for
        releasing the dispatch slot by calling ``reply_event.set()`` once the
        message has been consumed and the reply sent.

        The finally block in :meth:`_execute` ensures that if the manager or conversation
        task terminates without consuming all messages, any remaining dispatch slots
        are released to prevent deadlock. As long as :meth:`conversation_cb` doesn't
        raise, or doesn't call :meth:`asend`, ownership is not released.
        
        Args:
            msg: The received nng message.
            ctx: The context the message arrived on.
            reply_event: Event to set once the reply has been sent.
        """
        # If we're not running, we shouldn't be dispatching — reject immediately.
        if not self._running:
            reply_event.set()
            return

        # Push to inbox. Note the inbox should be empty since we are not expecting
        # to receive two message from the same client on two contexts. This can occur
        # though on two occasions:
        # - With resend_time a new request is made if the reply is not sent in time
        # - if the client misbehaves and sends a new request before receiving the reply
        #   to the previous one.
        # If we were to maintain the client liveness in both cases, the proper
        # behaviour is to discard the context of the previous message (not send the reply).
        # Indeed if we send the reply to the previous message, the REQ protocol on the client
        # side is supposed to trigger a close.
        # In this implementation we choose instead to reject any client displaying double message
        try:
            if self._inbox.qsize() != 0 or self._current_ctx is not None:
                raise RuntimeError("Client logic error: double message")
            self._inbox.put_nowait((msg, ctx, reply_event))
        except Exception:
            # If we failed to enqueue for any reason, release the slot
            reply_event.set()
            raise

    def _disconnect(self) -> None:
        """Place a ``None`` sentinel into the inbox to signal disconnection.

        The next :meth:`arecv` call will raise :exc:`ConnectionResetError`,
        unblocking whichever await is currently waiting.
        """
        self._running = False
        try:
            self._inbox.put_nowait(None)
        except asyncio.QueueShutDown:
            pass

    # ------------------------------------------------------------------
    # Raw transport (shared by handshake and application layer)
    # ------------------------------------------------------------------

    async def arecv(self) -> nng.Message:
        """Pop the next message from the inbox, record its context, and return the message.

        Returns:
            The received nng message.

        Raises:
            ConnectionResetError: If a ``None`` sentinel is dequeued (client disconnected).
        """
        # Pop the next item — None signals disconnection.
        item = await self._inbox.get()
        if item is None:
            raise ConnectionResetError("Client disconnected")

        # Record context so asend can reply on the correct context.
        msg, ctx, reply_event = item
        self._current_ctx = (ctx, reply_event)
        return msg

    async def asend(self, data: bytes | nng.Message) -> None:
        """Send message on the current context and release the dispatch slot.

        Args:
            data: Raw bytes or nng.Message to send (caller is responsible for encryption if needed).

        Raises:
            RuntimeError: If called without a preceding :meth:`arecv` or
                :meth:`_run` prime (no current context).
        """
        if self._current_ctx is None:
            raise RuntimeError("asend() called without a current context")

        # Capture and clear before the await so re-entrancy is safe.
        ctx, reply_event = self._current_ctx
        self._current_ctx = None

        try:
            await ctx.asend(data)
        except nng.NngError:
            raise ConnectionResetError(
                "Failed to send reply — client may have disconnected.\n"
                "Backtrace: " + traceback.format_exc()
                )
        finally:
            # Even on failure to send, the context considers
            # the arecv is consumed.
            reply_event.set()


class RepConversation:
    """Per-client conversation handle exposing asend/arecv.

    Created by :class:`_RepConversationManager`
    and passed as the sole argument to
    :meth:`RepServer.conversation`.

    All nng transport is delegated to the manager.

    Raises :exc:`ConnectionResetError` when the peer disconnects (propagated
    from :meth:`_RepConversationManager.arecv`).

    Not thread-safe.
    """

    __slots__ = {
        "_manager": "Transport manager handling the nng context and inbox queue.",
    }

    _manager: _RepConversationManager

    def __init__(
        self,
        manager: _RepConversationManager
    ) -> None:
        """Initialise with the transport manager.

        Args:
            manager: The per-client transport manager.
        """
        self._manager = manager

    # ------------------------------------------------------------------
    # Public property
    # ------------------------------------------------------------------

    @property
    def pipe(self) -> nng.Pipe:
        """The nng :class:`~nng.Pipe` identifying this client."""
        return self._manager._pipe  # type: ignore[reportPrivateUsage]

    # ------------------------------------------------------------------
    # Async send / recv
    # ------------------------------------------------------------------

    async def arecv(self) -> nng.Message:
        """Receive the next message from this client (async).

        Returns:
            The next nng.Message from the client.

        Raises:
            ConnectionResetError: If the client disconnected.
        """
        return await self._manager.arecv()

    async def asend(self, data: bytes | nng.Message) -> None:
        """Send data it as the reply to the current request (async).

        Args:
            data: bytes or nng.Message to send.

        Raises:
            RuntimeError: If called before a matching :meth:`arecv`.
            ConnectionResetError: If the client disconnected before or during the send.
        """
        await self._manager.asend(data)


class RepServer(ABC):
    """Abstract multi-client REP server with per-pipe conversation handles.

    Usage::

        class EchoServer(RepServer):
            async def conversation(self, conv: RepConversation) -> None:
                while True:
                    data = await conv.arecv()
                    await conv.asend(data)

        async def main() -> None:
            async with asyncio.TaskGroup() as tg:
                server = EchoServer()
                server.serve(tg, "tcp://127.0.0.1:5000")

    Not thread-safe.
    """

    __slots__ = {
        "_clients": "Dict mapping active pipes to their per-client managers.",
        "_loop": "Event loop captured at serve() for threadsafe nng callbacks.",
        "_socket": "The nng RepSocket.",
        "_tg": "TaskGroup passed to serve(); used to spawn per-client tasks.",
    }

    _clients: dict[nng.Pipe, _RepConversationManager]
    _loop: asyncio.AbstractEventLoop | None
    _socket: nng.RepSocket
    _tg: asyncio.TaskGroup | None

    def __init__(self, *args, **kwargs) -> None:
        """Initialise the server but do not bind yet.

        Arguments are passed through the contained RepSocket.
        """
        self._socket = nng.RepSocket(*args, **kwargs)
        self._clients = {}
        self._loop = None
        self._tg = None

    # ------------------------------------------------------------------
    # Abstract conversation entry point
    # ------------------------------------------------------------------

    @abstractmethod
    async def conversation(self, conv: RepConversation) -> None:
        """Handle one client session.

        This method should be implemented in a subclass to implement
        the application specific conversation logic. The conv object
        provides an already client with `asend`/`arecv`.

        Each client runs `conversation` inside a separate
        task, and all `asend` and `arecv` receive and send messages
        only to that client.

        If the client disconnects, `arecv` and `asend` will raise
        `ConnectionResetError`, which can be caught to perform cleanup.

        Return or raise from the conversation in order to terminate the
        client session and close the connection.

        Args:
            conv: Conversation handle providing `asend`/`arecv` for this
                specific client.
        """

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def serve(self, tg: asyncio.TaskGroup, url: str, *, n_contexts: int = 4) -> None:
        """Bind to *url* and start accepting clients using *tg*.

        Opens *n_contexts* independent nng contexts, each backed by its own
        dispatch task, so up to *n_contexts* requests can be in-flight
        concurrently.  Set *n_contexts* to the expected number of simultaneous
        clients.

        Args:
            tg: The :class:`asyncio.TaskGroup` that will own all per-client
                tasks and the dispatch loops.
            url: NNG transport URL to listen on (e.g. ``"tcp://0.0.0.0:5000"``).
            n_contexts: Number of concurrent receive/reply contexts.  Defaults
                to 4.

        Note: n_contexts does not limit the number of opened conversations
        (i.e. connected clients). Clients will just block until a context
        is free. Having a very large n_contexts doesn't impact performance,
        besides the _recv_dispatch task startup and cleanup times.
        """
        # Retrieve running loop for threadsafe callbacks
        self._loop = asyncio.get_running_loop()

        # Store task group reference so we can spawn per-client tasks
        self._tg = tg

        # Start listening and register the new-pipe callback to track clients.
        self._socket.on_new_pipe = self._on_new_pipe
        self._socket.add_listener(url).start()

        # Open n_context tasks to handle n_context simultaneous clients.
        for _ in range(n_contexts):
            ctx = self._socket.open_context()
            tg.create_task(self._recv_dispatch(ctx))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying socket, terminating all active connections."""
        self._socket.close()

    # ------------------------------------------------------------------
    # nng pipe callbacks (may fire from nng threads)
    # ------------------------------------------------------------------

    def _on_new_pipe(self, pipe: nng.Pipe) -> None:
        """Register a status-change callback on every new pipe.

        Args:
            pipe: The newly created pipe (status is ``ADDING`` at this point).
        """
        assert self._loop is not None
        pipe.on_status_change = lambda: self._loop.call_soon_threadsafe(  # type: ignore[union-attr]
            self._handle_pipe_status_change, pipe
        )

    def _handle_pipe_status_change(self, pipe: nng.Pipe) -> None:
        """Notify the per-client manager on pipe removal.

        Fires on the event loop (via
        :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`) for both
        ``ACTIVE`` and ``REMOVED`` transitions; only acts on ``REMOVED``.

        Args:
            pipe: The pipe whose status changed.
        """
        if pipe.status != nng.PipeStatus.REMOVED:
            return
        if mgr := self._clients.pop(pipe, None):
            mgr._disconnect()  # type: ignore[reportPrivateUsage]

    # ------------------------------------------------------------------
    # Central receive dispatch loop
    # ------------------------------------------------------------------

    async def _recv_dispatch(self, ctx: nng.Context) -> None:
        """Receive messages on *ctx*, route each one, then wait for the reply before looping.

        Reuses the same context for every receive/reply cycle.  After routing
        an inbound message to the appropriate manager, the task suspends on an
        :class:`asyncio.Event` set by
        :meth:`_RepConversationManager.asend` once the reply has
        been sent — ensuring *ctx* is free for the next
        :meth:`~nng.Context.arecv`.

        A done callback is registered on the per-client task before waiting so
        that if the task exits (error, timeout, disconnect) without setting the
        event, the dispatch slot is still released.

        If *ctx* enters an invalid state, the context is closed and a fresh one
        is opened so this dispatch slot can continue serving.

        The task exits cleanly when the socket is closed (:class:`~nng.NngClosed`).

        Args:
            ctx: The nng context to reuse for all receive/reply cycles.
        """
        assert self._tg is not None
        try:
            while True:
                try:
                    msg = await ctx.arecv()
                except nng.NngState:
                    # Context entered wrong state — recreate so this slot keeps serving.
                    ctx.close()
                    ctx = self._socket.open_context()
                    continue

                pipe = msg.pipe

                # No pipe — reply with an error to advance the REP state machine.
                if pipe is None:
                    await ctx.asend(b"No pipe")
                    continue

                # Create a handshape + conversation if the client doesn't exist yet
                client = self._clients.get(pipe, None)
                if client is None:
                    # Initialize conversation and spawn a dedicated client task
                    client = _RepConversationManager(pipe, self.conversation)
                    new_task = self._tg.create_task(client._execute())  # type: ignore[reportPrivateUsage]

                    # Add to known clients
                    self._clients[pipe] = client

                    # Remove from known clients when task exits
                    new_task.add_done_callback(
                        lambda _, p=pipe: self._clients.pop(p, None)
                    )

                # Route message to manager
                reply_event = asyncio.Event()
                try:
                    client._dispatch(msg, ctx, reply_event)  # type: ignore[reportPrivateUsage]
                except RuntimeError:
                    # Client logic error (e.g. double message) —  close.
                    pipe.close() # triggers _handle_pipe_status_change
                    await ctx.asend(b"") # frees the context
                    continue

                # Wait for context to be released (guaranteed by _dispatch,
                # unless the custom `conversation` has its own internal deadlock).
                await reply_event.wait()

        except nng.NngClosed:
            # Socket was closed — exit gracefully.
            return


# ---------------------------------------------------------------------------
# Start of the Demo (the code above can be reused/adapted for your application)
# ---------------------------------------------------------------------------

"""
Demo: stateful per-client calculator server.

Each client gets its own accumulator.  Supported commands (sent as UTF-8):

    add <n>   – add n to the accumulator
    sub <n>   – subtract n
    mul <n>   – multiply by n
    div <n>   – divide by n  (replies with "Error: …" on division by zero)
    reset     – reset accumulator to 0
    result    – query the current accumulator without changing it
    quit      – end the session cleanly

Replies are also plain UTF-8: the numeric result, or an error string.
"""

import argparse
import logging

DEFAULT_URL: str = "tcp://127.0.0.1:5555"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("calc-server")


class CalculatorServer(RepServer):
    """Stateful per-client calculator over the conversation pattern.

    Each connected client gets its own floating-point accumulator.
    The accumulator persists for the entire lifetime of the connection.
    """

    async def conversation(self, conv: RepConversation) -> None:
        """Handle one client session until it sends 'quit' or disconnects.

        Args:
            conv: Conversation handle providing ``asend``/``arecv`` for this
                specific client.
        """
        accumulator: float = 0.0
        peer_addr = conv.pipe.get_peer_addr()
        _LOG.info(f"Client {peer_addr} connected")

        # Minimal handshake to let clients know a new conversation
        # started in case of disconnection
        first_msg = await conv.arecv()
        if first_msg.to_bytes() != b"start conversation":
            await conv.asend(b"Expected 'start conversation' as the first message")
            return
        await conv.asend(b"ok")

        try:
            while True:
                msg = await conv.arecv()
                command = bytes(msg).decode("utf-8").strip()
                _LOG.debug("Client %s → %r", peer_addr, command)
    
                # Simulate a long-running command to demonstrate interleaving with other clients.
                # and the impact of the number of contexts
                await asyncio.sleep(1)

                # Parse and execute the command.
                reply = _handle_command(command, accumulator)
                if reply is None:
                    # 'quit' — acknowledge and end the conversation.
                    await conv.asend(b"bye")
                    break

                new_value, response = reply
                accumulator = new_value
                await conv.asend(response.encode("utf-8"))

        except ConnectionResetError:
            _LOG.info("Client %s disconnected", peer_addr)
        finally:
            _LOG.info("Client %s session ended  (accumulator=%.4g)", peer_addr, accumulator)


def _handle_command(
    command: str, accumulator: float
) -> tuple[float, str] | None:
    """Execute *command* against *accumulator*.

    Args:
        command: Raw command string from the client.
        accumulator: Current accumulator value.

    Returns:
        ``(new_accumulator, reply_string)`` on success, or ``None`` for 'quit'.
    """
    parts = command.split(maxsplit=1)
    op = parts[0].lower() if parts else ""

    # Quit sentinel.
    if op == "quit":
        return None

    # Query without mutation.
    if op == "result":
        return accumulator, f"{accumulator:.6g}"

    # Reset.
    if op == "reset":
        return 0.0, "0"

    # Arithmetic — all require a numeric argument.
    if op not in ("add", "sub", "mul", "div"):
        return accumulator, f"Error: unknown command {op!r}"

    if len(parts) < 2:
        return accumulator, f"Error: {op} requires a numeric argument"

    try:
        operand = float(parts[1])
    except ValueError:
        return accumulator, f"Error: {parts[1]!r} is not a number"

    if op == "add":
        new = accumulator + operand
    elif op == "sub":
        new = accumulator - operand
    elif op == "mul":
        new = accumulator * operand
    else:  # div
        if operand == 0:
            return accumulator, "Error: division by zero"
        new = accumulator / operand

    return new, f"{new:.6g}"


async def main(url: str, n_contexts: int) -> None:
    """Run the calculator server until interrupted.

    Args:
        url: NNG transport URL to listen on.
        n_contexts: Number of concurrent receive contexts.
    """
    _LOG.info("Calculator server listening on %s", url)
    server = CalculatorServer()
    try:
        async with asyncio.TaskGroup() as tg:
            server.serve(tg, url, n_contexts=n_contexts)
    except* (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        server.close()
        _LOG.info("Server shut down")


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Stateful per-client calculator server (conversation pattern demo)."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"NNG listen URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--contexts",
        type=int,
        default=4,
        metavar="N",
        help="Concurrent receive contexts (default: 4)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    _args = _parse_args()
    try:
        asyncio.run(main(_args.url, _args.contexts))
    except KeyboardInterrupt:
        print("\nInterrupted.")


"""
This module implements the server side of the secure REP/REQ pattern used in
the secure subscriber example.

The server is designed to handle multiple simultaneous clients, with each client
associated with a unique nng Pipe, and which handshake and conversation occur
in an exclusive asyncio task per client.

To use the server, subclass :class:`SecureRepServer` and implement the abstract
:meth:`conversation` method to perform the application-specific conversation with
the authenticated client.

Use :class: `SecureReqClient` to connect to the server.
"""

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import os
import traceback
from typing import Final, TypeAlias

import nacl.exceptions
import nacl.public
import nacl.signing

import nng

from .protocol_utils import (
    CipherBox,
    ErrorResponse
)

from .security_utils import (
    b64dec,
    generate_ephemeral_keypair,
    HandshakeClientConfirm,
    HandshakeClientHello,
    make_server_auth,
    make_session_box,
    verify_client_confirm
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default REP/REQ address for the subscription-request channel.
DEFAULT_REP_URL: Final[str] = "tcp://127.0.0.1:54325"

#: Handshake timeout in seconds applied on both sides.
HANDSHAKE_TIMEOUT_S: Final[float] = 5.0

#: Event emission interval in seconds.
DEFAULT_EVENT_INTERVAL_S: Final[float] = 0.5

_DispatchItem: TypeAlias = tuple[nng.Message, nng.Context, asyncio.Event]


@dataclass(frozen=True, kw_only=True)
class _ServerConfig:
    """Immutable configuration snapshot passed to each per-client manager.

    Attributes:
        authorized_vks: Set of Ed25519 verify keys allowed to connect.
        conversation_cb: Coroutine called with the conversation handle after the handshake.
        server_signing_key: Server long-term Ed25519 signing key.
        timeout: Per-client handshake timeout in seconds.
    """

    authorized_vks: frozenset[nacl.signing.VerifyKey]
    conversation_cb: Callable[[_SecureReqConversation], Awaitable[None]]
    server_signing_key: nacl.signing.SigningKey
    timeout: float


class _SecureReqConversationManager:
    """Per-client nng transport manager for :class:`SecureRepServer`.

    Owns the inbox queue, current-context tracking, and the per-client task.
    Performs the three-message handshake then hands off to the application via
    :attr:`_ServerConfig.conversation_cb`.

    All crypto is owned by :class:`_SecureReqConversation`; this class is
    entirely phase-agnostic.

    Not thread-safe.
    """

    __slots__ = {
        "_config": "Immutable server configuration.",
        "_current_ctx": "Context and reply event from the last _arecv_raw, cleared by _asend_raw.",
        "_inbox": "Single message queue for all phases (handshake and application).",
        "_pipe": "The nng Pipe identifying the remote client.",
        "_running": "Whether the manager task is still running (used to prevent dispatching after disconnect).",
    }

    _config: _ServerConfig
    _current_ctx: tuple[nng.Context, asyncio.Event] | None
    _inbox: asyncio.Queue[_DispatchItem | None]
    _pipe: nng.Pipe
    _running: bool

    def __init__(self, pipe: nng.Pipe, config: _ServerConfig) -> None:
        """Initialise the manager for *pipe*.

        Args:
            pipe: The nng pipe identifying the remote client.
            config: Immutable server configuration.
        """
        self._config = config
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
        """Perform the handshake then invoke the conversation callback.

        Uses :meth:`_arecv_raw` and :meth:`_asend_raw` throughout so that
        ``_current_ctx`` management and reply-event release are centralised in
        a single place.

        Args:
            hello_msg: The first message received from the client (ClientHello).
        """
        # Start timed handshake.
        config = self._config
        try:
            async with asyncio.timeout(config.timeout):
                # Retrieve the initial client message
                hello_msg = await self._arecv_raw()

                # Parse ClientHello.
                hello = HandshakeClientHello.from_json(hello_msg)
                client_ephem_pub = nacl.public.PublicKey(b64dec(hello.client_ephem_pub_b64))
                client_challenge = b64dec(hello.client_challenge_b64)

                # Generate server ephemeral keys and derive the session box.
                server_ephem_priv, server_ephem_pub = generate_ephemeral_keypair()
                server_challenge = os.urandom(32)
                box = make_session_box(server_ephem_priv, client_ephem_pub)

                # Reply with ServerAuth — _asend_raw releases the hello dispatch slot.
                await self._asend_raw(
                    make_server_auth(
                        client_ephem_pub, client_challenge,
                        server_ephem_pub, server_challenge,
                        config.server_signing_key,
                    ).to_json()
                )

                # Wait for ClientConfirm — None means client disconnected.
                raw = await self._arecv_raw()

                # Decrypt and verify against all authorised verify keys.
                raw_confirm = box.decrypt(raw)
                confirm = HandshakeClientConfirm.from_json(raw_confirm)

                # Check the signature against all authorized verify keys.
                try:
                    verify_client_confirm(
                        confirm, config.authorized_vks, server_challenge, client_challenge,
                        server_ephem_pub, client_ephem_pub,
                    )
                except nacl.exceptions.BadSignatureError:
                    await self._asend_raw(ErrorResponse(reason="Unauthorized client").to_json())
                    return


                # Send handshake acknowledgement — releases the confirm dispatch slot.
                await self._asend_raw(b"ok")

            # Run the conversation callback.
            conv = _SecureReqConversation(self, box)
            try:
                await config.conversation_cb(conv)
            except ConnectionResetError:
                pass

        except ConnectionResetError:
            pass  # Client disconnected during handshake — normal for rejected clients.

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
    # Dispatch interface (called by SecureRepServer._recv_dispatch)
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        msg: nng.Message,
        ctx: nng.Context,
        reply_event: asyncio.Event,
    ) -> None:
        """Enqueue an inbound message for :meth:`_execute` to consume via :meth:`_arecv_raw`.

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
        # to receive two message from the same client on two contexts (nng guarantee).
        try:
            if self._inbox.qsize() != 0:
                raise RuntimeError("Internal logic error: expected empty inbox")
            self._inbox.put_nowait((msg, ctx, reply_event))
        except Exception:
            # If we failed to enqueue for any reason, release the slot
            reply_event.set()
            raise

    def _disconnect(self) -> None:
        """Place a ``None`` sentinel into the inbox to signal disconnection.

        The next :meth:`_arecv_raw` call will raise :exc:`ConnectionResetError`,
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

    async def _arecv_raw(self) -> bytes:
        """Pop the next message from the inbox, record its context, and return raw bytes.

        Returns:
            Raw (unencrypted) message bytes.

        Raises:
            ConnectionResetError: If a ``None`` sentinel is dequeued (client disconnected).
        """
        # Pop the next item — None signals disconnection.
        item = await self._inbox.get()
        if item is None:
            raise ConnectionResetError("Client disconnected")

        # Record context so _asend_raw can reply on the correct context.
        msg, ctx, reply_event = item
        self._current_ctx = (ctx, reply_event)
        return msg.to_bytes()

    async def _asend_raw(self, data: bytes) -> None:
        """Send raw bytes on the current context and release the dispatch slot.

        Args:
            data: Raw bytes to send (caller is responsible for encryption if needed).

        Raises:
            RuntimeError: If called without a preceding :meth:`_arecv_raw` or
                :meth:`_run` prime (no current context).
        """
        if self._current_ctx is None:
            raise RuntimeError("_asend_raw() called without a current context")

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


class _SecureReqConversation:
    """Per-client conversation handle exposing encrypted send/recv.

    Created by :class:`_SecureReqConversationManager` once the handshake
    completes and passed as the sole argument to
    :meth:`SecureRepServer.conversation`.

    All nng transport is delegated to the manager; this class owns only the
    crypto layer (session box + encrypt/decrypt).

    Raises :exc:`ConnectionResetError` when the peer disconnects (propagated
    from :meth:`_SecureReqConversationManager._arecv_raw`).

    Not thread-safe.
    """

    __slots__ = {
        "_box": "Session authenticated-encryption box.",
        "_manager": "Transport manager handling the nng context and inbox queue.",
    }

    _box: CipherBox
    _manager: _SecureReqConversationManager

    def __init__(
        self,
        manager: _SecureReqConversationManager,
        box: CipherBox,
    ) -> None:
        """Initialise with the transport manager and session box.

        Args:
            manager: The per-client transport manager.
            box: The session encryption box derived during the handshake.
        """
        self._manager = manager
        self._box = box

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

    async def arecv(self) -> bytes:
        """Receive and decrypt the next message from this client (async).

        Returns:
            Decrypted payload bytes.

        Raises:
            ConnectionResetError: If the client disconnected.
            nacl.exceptions.CryptoError: If decryption or authentication fails.
        """
        raw = await self._manager._arecv_raw()  # type: ignore[reportPrivateUsage]
        return self._box.decrypt(raw)

    async def asend(self, data: bytes) -> None:
        """Encrypt *data* and send it as the reply to the current request (async).

        Args:
            data: Plaintext bytes to encrypt and send.

        Raises:
            RuntimeError: If called before a matching :meth:`arecv`.
            ConnectionResetError: If the client disconnected before or during the send.
            nacl.exceptions.CryptoError: If encryption fails.
        """
        await self._manager._asend_raw(self._box.encrypt(data))  # type: ignore[reportPrivateUsage]


class SecureRepServer(ABC):
    """Abstract multi-client REP server with per-pipe authenticated encryption.

    Each connecting client must complete the three-message client-first handshake
    (``ClientHello`` → ``ServerAuth`` → ``ClientConfirm``).  After the handshake
    succeeds the abstract :meth:`conversation` method is called in the same task
    that performed the handshake, with a :class:`_SecureReqConversation` handle
    that provides encrypted :meth:`~_SecureReqConversation.arecv` /
    :meth:`~_SecureReqConversation.asend` for that specific client.

    Usage::

        class EchoServer(SecureRepServer):
            async def conversation(self, conv: _SecureReqConversation) -> None:
                while True:
                    data = await conv.arecv()
                    await conv.asend(data)

        async def main() -> None:
            async with asyncio.TaskGroup() as tg:
                server = EchoServer(server_signing_key, authorized_vks)
                server.serve(tg, "tcp://127.0.0.1:5000")

    Not thread-safe.
    """

    __slots__ = {
        "_authorized_vks": "Frozen set of authorized client Ed25519 verify keys.",
        "_clients": "Dict mapping active pipes to their per-client managers.",
        "_config": "Server configuration built in serve(); None before serve() is called.",
        "_loop": "Event loop captured at serve() for threadsafe nng callbacks.",
        "_server_signing_key": "Server long-term Ed25519 signing key.",
        "_socket": "The nng RepSocket.",
        "_timeout": "Per-client handshake timeout in seconds.",
        "_tg": "TaskGroup passed to serve(); used to spawn per-client tasks.",
    }

    _authorized_vks: frozenset[nacl.signing.VerifyKey]
    _clients: dict[nng.Pipe, _SecureReqConversationManager]
    _config: _ServerConfig | None
    _loop: asyncio.AbstractEventLoop | None
    _server_signing_key: nacl.signing.SigningKey
    _socket: nng.RepSocket
    _timeout: float
    _tg: asyncio.TaskGroup | None

    def __init__(
        self,
        server_signing_key: nacl.signing.SigningKey,
        authorized_vks: frozenset[nacl.signing.VerifyKey],
        *,
        timeout: float = HANDSHAKE_TIMEOUT_S,
    ) -> None:
        """Initialise the server but do not bind yet.

        Call :meth:`serve` once inside an ``asyncio.TaskGroup`` to start
        accepting connections.

        Args:
            server_signing_key: The server's long-term Ed25519 signing key.
            authorized_vks: Frozen set of client Ed25519 verify keys that are
                allowed to connect.
            timeout: Per-client handshake timeout in seconds.
        """
        self._server_signing_key = server_signing_key
        self._authorized_vks = authorized_vks
        self._timeout = timeout
        self._socket = nng.RepSocket()
        self._clients = {}
        self._config = None
        self._loop = None
        self._tg = None

    # ------------------------------------------------------------------
    # Abstract conversation entry point
    # ------------------------------------------------------------------

    @abstractmethod
    async def conversation(self, conv: _SecureReqConversation) -> None:
        """Handle one authenticated client session.

        This method should be implemented in a subclass to implement
        the application specific conversation logic. The conv object
        provides an already authenticated client with encrypted `asend`/`arecv`.

        Each authenticated client runs `conversation` inside a separate
        task, and all `asend` and `arecv` receive and send messages
        only to that client.

        If the client disconnects, `arecv` and `asend` will raise
        `ConnectionResetError`, which can be caught to perform cleanup.

        Return or raise from the conversation in order to terminate the
        client session and close the connection.

        Args:
            conv: Conversation handle providing encrypted `asend`/`arecv` for this
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
        """
        # Retrieve running loop for threadsafe callbacks
        self._loop = asyncio.get_running_loop()

        # Store task group reference so we can spawn per-client tasks
        self._tg = tg

        # Build the immutable config snapshot to pass to managers
        self._config = _ServerConfig(
            authorized_vks=self._authorized_vks,
            conversation_cb=self.conversation,
            server_signing_key=self._server_signing_key,
            timeout=self._timeout,
        )

        # Start listening and register the new-pipe callback to track clients.
        self._socket.on_new_pipe(self._on_new_pipe)
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
        :meth:`_SecureReqConversationManager._asend_raw` once the reply has
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
        assert self._config is not None
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
                    await ctx.asend(ErrorResponse(reason="No pipe").to_json())
                    continue

                # Create a handshape + conversation if the client doesn't exist yet
                client = self._clients.get(pipe, None)
                if client is None:
                    # Initialize conversation and spawn a dedicated client task
                    client = _SecureReqConversationManager(pipe, self._config)
                    new_task = self._tg.create_task(client._execute())  # type: ignore[reportPrivateUsage]

                    # Add to known clients
                    self._clients[pipe] = client

                    # Remove from known clients when task exits
                    new_task.add_done_callback(
                        lambda _, p=pipe: self._clients.pop(p, None)
                    )

                # Route message to manager
                reply_event = asyncio.Event()
                client._dispatch(msg, ctx, reply_event)  # type: ignore[reportPrivateUsage]

                # Wait for context to be released (guaranteed by _dispatch,
                # unless the custom `conversation` has its own internal deadlock).
                await reply_event.wait()

        except nng.NngClosed:
            # Socket was closed — exit gracefully.
            return

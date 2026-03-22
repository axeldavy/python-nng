"""
This module implements the client side of the secure REP/REQ pattern used in
the secure subscriber example.

The client is designed to connect to a single server, and close the socket
when the server disconnects. Communication with the server should occur
inside a `with` / `async with` context manager, which ensures the handshake
is completed before sending or receiving messages, and that the socket is
closed when done.

Use :class: `SecureRepServer` for the server.
"""

from __future__ import annotations

import asyncio
import os
from typing import Self

import nacl.public
import nacl.signing

import nng

from .protocol_utils import (
    CipherBox,
    HANDSHAKE_TIMEOUT_S
)

from .security_utils import (
    generate_ephemeral_keypair,
    HandshakeServerAuth,
    make_client_confirm,
    make_client_hello,
    make_session_box,
    verify_server_auth
)


class SecureReqClient:
    """Client that dials a :class:`SecureRepServer` and exchanges encrypted messages.

    Performs the same three-message client-first handshake as
    :class:`SecurePairClient`, but over a :class:`~nng.ReqSocket` (REQ/REP)
    instead of a PAIR socket.  After the handshake every ``send`` / ``recv``
    pair encrypts / decrypts transparently.

    Use as a context manager (sync or async) — entry connects and completes
    the handshake; exit closes the socket.

    Not thread-safe.
    """

    __slots__ = {
        "_box": "Session encryption box, set after handshake.",
        "_client_ephem_priv": "Client X25519 ephemeral private key.",
        "_client_ephem_pub": "Client X25519 ephemeral public key.",
        "_client_signing_key": "Client long-term Ed25519 signing key.",
        "_pipe": "NNG pipe associated with the current conversation.",
        "_rep_addr": "NNG URL of the server REP socket.",
        "_server_verify_key": "Server Ed25519 verify key (pinned).",
        "_socket": "Underlying nng ReqSocket.",
        "_timeout": "Handshake timeout in seconds.",
    }

    _box: CipherBox | None
    _client_ephem_priv: nacl.public.PrivateKey
    _client_ephem_pub: nacl.public.PublicKey
    _client_signing_key: nacl.signing.SigningKey
    _pipe: nng.Pipe | None
    _rep_addr: str
    _server_verify_key: nacl.signing.VerifyKey
    _socket: nng.ReqSocket
    _timeout: float

    def __init__(
        self,
        rep_addr: str,
        client_signing_key: nacl.signing.SigningKey,
        server_verify_key: nacl.signing.VerifyKey,
        *,
        timeout: float = HANDSHAKE_TIMEOUT_S,
    ) -> None:
        """Prepare the client but do not connect yet.

        Call via the context manager (``with`` / ``async with``) to connect
        and complete the handshake.

        Args:
            rep_addr: NNG URL of the server's REP socket.
            client_signing_key: Client long-term Ed25519 signing key.
            server_verify_key: Server Ed25519 verify key (pinned at construction).
            timeout: Handshake timeout in seconds.
        """
        self._rep_addr = rep_addr
        self._client_signing_key = client_signing_key
        self._server_verify_key = server_verify_key
        self._timeout = timeout
        self._box = None
        self._pipe = None
        self._socket = nng.ReqSocket()
        self._socket.on_new_pipe(self._register_pipe)
        self._client_ephem_priv, self._client_ephem_pub = generate_ephemeral_keypair()

    # ------------------------------------------------------------------
    # Handshake helpers
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Dial the server and complete the handshake (sync).

        Raises:
            nacl.exceptions.BadSignatureError: On server authentication failure.
            nng.NngError: On transport error.
        """
        self._socket.add_dialer(self._rep_addr).start(block=False)

        # Send ClientHello — first REQ (unauthenticated; nothing from server yet).
        client_challenge = os.urandom(32)
        self._socket.send(
            make_client_hello(self._client_ephem_pub, client_challenge).to_json()
        )

        # Receive response
        server_response = self._socket.recv()

        # Register server pipe
        self._register_pipe(server_response.pipe)

        # Receive and verify ServerAuth.
        server_ephem_pub, server_challenge = verify_server_auth(
            HandshakeServerAuth.from_json(server_response.to_bytes()),
            self._server_verify_key,
            self._client_ephem_pub,
            client_challenge,
        )

        # Derive the session box.
        self._box = make_session_box(self._client_ephem_priv, server_ephem_pub)

        # Send encrypted ClientConfirm — second REQ.
        self._socket.send(
            self._box.encrypt(
                make_client_confirm(
                    server_challenge, client_challenge,
                    server_ephem_pub, self._client_ephem_pub,
                    self._client_signing_key,
                ).to_json()
            )
        )

        # Receive handshake acknowledgement.
        server_response = self._socket.recv()
        self._register_pipe(server_response.pipe)

    async def _aconnect(self) -> None:
        """Dial the server and complete the handshake (async).

        Raises:
            nacl.exceptions.BadSignatureError: On server authentication failure.
            TimeoutError: If the handshake exceeds :attr:`_timeout` seconds.
            nng.NngError: On transport error.
        """
        self._socket.add_dialer(self._rep_addr).start(block=False)

        async with asyncio.timeout(self._timeout):
            # Send ClientHello — first REQ.
            client_challenge = os.urandom(32)
            await self._socket.asend(
                make_client_hello(self._client_ephem_pub, client_challenge).to_json()
            )

            # Receive response
            server_response = await self._socket.arecv()

            # Register server pipe
            self._register_pipe(server_response.pipe)

            # Receive and verify ServerAuth.
            server_ephem_pub, server_challenge = verify_server_auth(
                HandshakeServerAuth.from_json(server_response.to_bytes()),
                self._server_verify_key,
                self._client_ephem_pub,
                client_challenge,
            )

            # Derive the session box.
            self._box = make_session_box(self._client_ephem_priv, server_ephem_pub)

            # Send encrypted ClientConfirm — second REQ.
            await self._socket.asend(
                self._box.encrypt(
                    make_client_confirm(
                        server_challenge, client_challenge,
                        server_ephem_pub, self._client_ephem_pub,
                        self._client_signing_key,
                    ).to_json()
                )
            )

            # Receive handshake acknowledgement.
            server_response = await self._socket.arecv()
            self._register_pipe(server_response.pipe)

    def _close(self) -> None:
        """Close the underlying socket."""
        self._socket.close()

    # ------------------------------------------------------------------
    # Context manager — sync
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        """Connect and complete the handshake.

        Returns:
            *self*
        """
        self._connect()
        return self

    def __exit__(self, *_: object) -> None:
        """Close the socket."""
        self._close()

    # ------------------------------------------------------------------
    # Context manager — async
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Connect and complete the handshake asynchronously.

        Returns:
            *self*
        """
        await self._aconnect()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the socket."""
        self._close()


    # ------------------------------------------------------------------
    # Pipe management
    # ------------------------------------------------------------------

    def _register_pipe(self, pipe: nng.Pipe | None) -> None:
        """Register the new pipe and reject a second peer.

        Args:
            pipe: The new pipe from nng, or ``None`` on unexpected disconnect.

        Raises:
            ValueError: If *pipe* is ``None``.
            PermissionError: If a different pipe was already registered.
        """
        # Handle message with no pipe
        if pipe is None:
            self._socket.close()
            raise ValueError("Invalid client behavior: received message with no Pipe")

        # Handle new pipe (which we disallow)
        if self._pipe is not None and self._pipe != pipe:
            pipe.close()
            self._socket.close()
            raise PermissionError("Invalid client behavior: multiple pipes detected")

        # Handle first pipe
        if self._pipe is None:
            self._pipe = pipe
            pipe.on_status_change = self._handle_pipe_status

    def _handle_pipe_status(self) -> None:
        """Close the socket when the peer disconnects."""
        if self._pipe is None:
            return

        # Handle disconnection
        if self._pipe.status == nng.PipeStatus.REMOVED:
            self._socket.close()
            self._pipe = None

    @property
    def pipe(self) -> nng.Pipe | None:
        """The currently registered Pipe, or ``None`` if no peer is connected."""
        return self._pipe

    # ------------------------------------------------------------------
    # Encrypted send / recv
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        """Encrypt *data* and send it as a request (sync).

        Args:
            data: Plaintext bytes to encrypt and transmit.

        Raises:
            RuntimeError: If called before the handshake completes.
        """
        if self._box is None:
            raise RuntimeError("Cannot send before handshake completes (use with/async with)")
        self._socket.send(self._box.encrypt(data))

    def recv(self) -> bytes:
        """Receive and decrypt one reply (sync).

        Returns:
            Decrypted payload bytes.

        Raises:
            RuntimeError: If called before the handshake completes.
            nacl.exceptions.CryptoError: If authentication or decryption fails.
        """
        if self._box is None:
            raise RuntimeError("Cannot recv before handshake completes (use with/async with)")

        # Receive message
        message = self._socket.recv()

        # Check pipe
        self._register_pipe(message.pipe)

        # Decrypt and return the payload
        return self._box.decrypt(message.to_bytes())

    async def asend(self, data: bytes) -> None:
        """Encrypt *data* and send it as a request (async).

        Args:
            data: Plaintext bytes to encrypt and transmit.

        Raises:
            RuntimeError: If called before the handshake completes.
        """
        if self._box is None:
            raise RuntimeError("Cannot send before handshake completes (use with/async with)")
        await self._socket.asend(self._box.encrypt(data))

    async def arecv(self) -> bytes:
        """Receive and decrypt one reply (async).

        Returns:
            Decrypted payload bytes.

        Raises:
            RuntimeError: If called before the handshake completes.
            nacl.exceptions.CryptoError: If authentication or decryption fails.
        """
        if self._box is None:
            raise RuntimeError("Cannot recv before handshake completes (use with/async with)")

        # Receive message
        message = await self._socket.arecv()

        # Check pipe
        self._register_pipe(message.pipe)

        # Decrypt and return the payload
        return self._box.decrypt(message.to_bytes())

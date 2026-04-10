"""
secure_pair.py — high-level secure PAIR socket wrappers for the subscriber demo.

This module provides :class:`SecurePairServer` and :class:`SecurePairClient`,
which implement the mutual-auth handshake and encrypted messaging over PAIR sockets for
the subscriber demo. The handshake is based on the same ephemeral X25519 key exchange
and Ed25519 signatures as the REP/REQ handshake, but adapted to the PAIR socket's direct peer-to-peer nature.

The server binds a PAIR listener and waits for one client to connect; the client dials the
known PAIR address and they perform the handshake.  After the handshake, both sides can
use the provided :meth:`send` and :meth:`recv` methods to exchange encrypted messages.

"""

from abc import ABC, abstractmethod
import asyncio
import os
from typing import Self, TypeVar, override

import nacl.public
import nacl.signing

import nng

from .protocol_utils import (
    CipherBox,
    HANDSHAKE_TIMEOUT_S
)

from .security_utils import (
    b64dec,
    generate_ephemeral_keypair,
    HandshakeClientConfirm,
    HandshakeClientHello,
    HandshakeServerAuth,
    make_client_confirm,
    make_client_hello,
    make_server_auth,
    make_session_box,
    verify_client_confirm,
    verify_server_auth
)


# ---------------------------------------------------------------------------
# High-level secure PAIR socket wrappers
# ---------------------------------------------------------------------------

_ServerT = TypeVar("_ServerT", bound="SecurePairServer")


class BaseSecurePair(ABC):
    """Base class for a PAIR socket with handshake and encryption.

    This abstract class implements the common logic for both server and client
    PAIR sockets, including:
    - Disconnecting when the peer disconnects
    - Rejecting new peers when already connected
    - Encrypting/decrypting messages after the handshake (using a subclass provided item)
    - Handshake occuring inside the context manager entry, so the socket is only usable
        when the handshake completes successfully.

    Subclasses implement :meth:`_connect` and :meth:`_aconnect` to perform the
    key exchange and set :attr:`_box` before the application layer can send or
    receive.  Entering the context manager calls those methods; exiting calls
    :meth:`_close`.

    Not thread-safe.
    """

    __slots__ = {
        "_box": "NaCl Box derived after the handshake.",
        "_pair": "Underlying nng PairSocket.",
        "_pipe": "Remote peer's Pipe object.",
        "_timeout": "Handshake timeout in seconds.",
    }

    _box: CipherBox | None
    _pair: nng.PairSocket
    _pipe: nng.Pipe | None
    _timeout: float

    def __init__(
        self,
        pair: nng.PairSocket,
        *,
        timeout: float = HANDSHAKE_TIMEOUT_S,
    ) -> None:
        """Store the PAIR socket and register the pipe callback.

        Args:
            pair: The PAIR socket (already bound, or to be dialled by the
                subclass during :meth:`_connect` / :meth:`_aconnect`).
            timeout: Handshake timeout in seconds.
        """
        self._box = None
        self._pair = pair
        self._timeout = timeout
        self._pipe = None
        pair.on_new_pipe = self._register_pipe

    # ------------------------------------------------------------------
    # Abstract connection entry points (provided by each concrete subclass)
    # ------------------------------------------------------------------

    @abstractmethod
    def _connect(self) -> None:
        """Perform the synchronous handshake and set :attr:`_box`.

        Raises:
            nacl.exceptions.BadSignatureError: On any authentication failure.
            nng.NngError: On transport error.
        """

    @abstractmethod
    async def _aconnect(self) -> None:
        """Perform the async handshake and set :attr:`_box`.

        Raises:
            nacl.exceptions.BadSignatureError: On any authentication failure.
            TimeoutError: If the handshake exceeds :attr:`_timeout` seconds.
            nng.NngError: On transport error.
        """

    # ------------------------------------------------------------------
    # Virtual close hook
    # ------------------------------------------------------------------

    def _close(self) -> None:
        """Close all sockets. Override to close additional sockets."""
        self._pair.close()

    def close(self) -> None:
        """Public close method to allow manual cleanup outside of context manager."""
        self._close()

    # ------------------------------------------------------------------
    # Context manager — sync
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        """Call :meth:`_connect` and return *self*.

        Returns:
            *self*
        """
        self._connect()
        return self

    def __exit__(self, *_: object) -> None:
        """Call :meth:`_close`."""
        self._close()

    # ------------------------------------------------------------------
    # Context manager — async
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Call :meth:`_aconnect` and return *self*.

        Returns:
            *self*
        """
        await self._aconnect()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Call :meth:`_close`."""
        self._close()

    # ------------------------------------------------------------------
    # Pipe management (shared)
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
            self._pair.close()
            raise ValueError("Invalid client behavior: received message with no Pipe")

        # Handle new pipe (which we disallow)
        if self._pipe is not None and self._pipe != pipe:
            pipe.close()
            self._pair.close()
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
            self._pair.close()
            self._pipe = None

    @property
    def pipe(self) -> nng.Pipe | None:
        """The currently registered Pipe, or ``None`` if no peer is connected."""
        return self._pipe

    # ------------------------------------------------------------------
    # Encrypted send / recv
    # ------------------------------------------------------------------

    def send(self, payload: bytes) -> None:
        """Encrypt *payload* and send it over the PAIR socket.

        Args:
            payload: Bytes to encrypt and transmit.
        """
        if self._box is None:
            raise RuntimeError("Cannot send before handshake completes (use with/async with)")
        self._pair.send(self._box.encrypt(payload))

    def recv(self) -> bytes:
        """Receive and decrypt one message from the PAIR socket.

        Returns:
            The decrypted payload bytes.

        Raises:
            nacl.exceptions.CryptoError: If authentication or decryption fails.
        """
        if self._box is None:
            raise RuntimeError("Cannot receive before handshake completes (use with/async with)")

        # Receive message
        message = self._pair.recv()

        # Check pipe
        self._register_pipe(message.pipe)

        # Decrypt and return the payload
        return self._box.decrypt(message.to_bytes())

    async def asend(self, payload: bytes) -> None:
        """Encrypt *payload* and send it asynchronously.

        Args:
            payload: Bytes to encrypt and transmit.
        """
        if self._box is None:
            raise RuntimeError("Cannot send before handshake completes (use with/async with)")
        await self._pair.asend(self._box.encrypt(payload))

    async def arecv(self) -> bytes:
        """Receive and decrypt one message asynchronously.

        Returns:
            The decrypted payload bytes.

        Raises:
            nacl.exceptions.CryptoError: If authentication or decryption fails.
        """
        if self._box is None:
            raise RuntimeError("Cannot receive before handshake completes (use with/async with)")

        # Receive message
        message = await self._pair.arecv()

        # Check pipe
        self._register_pipe(message.pipe)

        # Decrypt and return the payload
        return self._box.decrypt(message.to_bytes())



class SecurePairServer(BaseSecurePair):
    """Server-side PAIR socket performing the mutual-auth challenge-response.

    Create an instance via the :meth:`create` async factory, which generates
    ephemeral keys, binds the PAIR listener, and returns an instance ready to
    be used as a context manager.  On entry the context manager completes the
    PAIR-level handshake; on exit it closes the socket.

    For workflows where a ``SubscribeResponse`` must also be sent over a REP
    socket, use :class:`EphemeralPairServer` instead.

    Not thread-safe.
    """

    __slots__ = {
        "_authorized_vks": "Frozen set of authorized client Ed25519 verify keys.",
        "_pair_addr": "NNG URL of the bound PAIR listener.",
        "_server_signing_key": "Server long-term Ed25519 signing key.",
    }
    _authorized_vks: frozenset[nacl.signing.VerifyKey]
    _client_vk: nacl.signing.VerifyKey
    _pair_addr: str
    _server_signing_key: nacl.signing.SigningKey

    def __init__(
        self,
        pair: nng.PairSocket,
        authorized_vks: frozenset[nacl.signing.VerifyKey],
        server_signing_key: nacl.signing.SigningKey,
        *,
        pair_addr: str,
        timeout: float = HANDSHAKE_TIMEOUT_S,
    ) -> None:
        """Initialise from an already-bound PAIR socket and pre-generated keys.

        Prefer the :meth:`create` factory which handles all these steps.

        Args:
            pair: PAIR socket already bound and listening.
            authorized_vks: Frozen set of authorized client Ed25519 verify keys.
            server_signing_key: Server's long-term Ed25519 signing key.
            pair_addr: NNG URL of the bound PAIR listener (stored so subclasses
                such as :class:`EphemeralPairServer` can read it after
                :meth:`create` returns).
            timeout: Handshake timeout in seconds.
        """
        super().__init__(pair, timeout=timeout)
        self._authorized_vks = authorized_vks
        self._server_signing_key = server_signing_key
        self._pair_addr = pair_addr

    @property
    def pair_addr(self) -> str:
        """NNG URL of the bound PAIR listener."""
        return self._pair_addr

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls: type[_ServerT],
        authorized_vks: frozenset[nacl.signing.VerifyKey],
        server_signing_key: nacl.signing.SigningKey,
        pair_addr: str,
        *,
        timeout: float = HANDSHAKE_TIMEOUT_S,
    ) -> _ServerT:
        """Bind a PAIR listener and return a ready server.

        The client's ephemeral key is learned during the handshake (from the
        client hello), so it does not need to be passed here.

        Args:
            authorized_vks: Frozen set of authorized Ed25519 client verify keys.
            server_signing_key: Server's long-term Ed25519 signing key.
            pair_addr: NNG URL of the bound PAIR listener.
            timeout: Handshake timeout passed to the returned instance.

        Returns:
            A :class:`SecurePairServer` (or subclass) ready for context-manager
            entry.
        """
        # Bind the PAIR listener before notifying the client.
        pair: nng.PairSocket = nng.PairSocket()
        pair.add_listener(pair_addr).start()

        return cls(
            pair,
            authorized_vks=authorized_vks,
            server_signing_key=server_signing_key,
            pair_addr=pair_addr,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Handshake — sync
    # ------------------------------------------------------------------

    @override
    def _connect(self) -> None:
        """Perform the synchronous server-side PAIR handshake.

        Waits for the client hello, generates ephemeral material, sends
        :class:`~security_utils.HandshakeServerAuth`, derives :attr:`_box`,
        then receives and verifies the encrypted
        :class:`~security_utils.HandshakeClientConfirm`.

        Raises:
            nacl.exceptions.BadSignatureError: If the client confirm is invalid.
            nng.NngError: On transport error.
        """
        # Receive client hello.
        hello = HandshakeClientHello.from_json(self._pair.recv().to_bytes())
        client_ephem_pub = nacl.public.PublicKey(b64dec(hello.client_ephem_pub_b64))
        client_challenge = b64dec(hello.client_challenge_b64)

        # Generate session-ephemeral material.
        server_challenge = os.urandom(32)
        server_ephem_priv, server_ephem_pub = generate_ephemeral_keypair()

        # Derive box now — both ephemeral keys are known.
        self._box = make_session_box(server_ephem_priv, client_ephem_pub)

        # Send server auth.
        self._pair.send(make_server_auth(
            client_ephem_pub, client_challenge,
            server_ephem_pub, server_challenge,
            self._server_signing_key,
        ).to_json())

        # Receive encrypted client confirm and verify.
        verify_client_confirm(
            HandshakeClientConfirm.from_json(self.recv()),
            self._authorized_vks,
            server_challenge, client_challenge,
            server_ephem_pub, client_ephem_pub,
        )

    @override
    async def _aconnect(self) -> None:
        """Perform the async server-side PAIR handshake.

        Raises:
            nacl.exceptions.BadSignatureError: If the client confirm is invalid.
            TimeoutError: If the handshake exceeds :attr:`_timeout` seconds.
            nng.NngError: On transport error.
        """
        async with asyncio.timeout(self._timeout):
            # Receive client hello.
            hello = HandshakeClientHello.from_json((await self._pair.arecv()).to_bytes())
            client_ephem_pub = nacl.public.PublicKey(b64dec(hello.client_ephem_pub_b64))
            client_challenge = b64dec(hello.client_challenge_b64)

            # Generate session-ephemeral material.
            server_challenge = os.urandom(32)
            server_ephem_priv, server_ephem_pub = generate_ephemeral_keypair()

            # Derive box now — both ephemeral keys are known.
            self._box = make_session_box(server_ephem_priv, client_ephem_pub)

            # Send server auth.
            await self._pair.asend(make_server_auth(
                client_ephem_pub, client_challenge,
                server_ephem_pub, server_challenge,
                self._server_signing_key,
            ).to_json())

            # Receive encrypted client confirm and verify.
            verify_client_confirm(
                HandshakeClientConfirm.from_json(await self.arecv()),
                self._authorized_vks,
                server_challenge, client_challenge,
                server_ephem_pub, client_ephem_pub,
            )


class SecurePairClient(BaseSecurePair):
    """Concrete client-side secure PAIR wrapper for direct server connections.

    Dials a known PAIR address and completes the mutual-auth handshake.
    For the REP/REQ subscription workflow use :class:`EphemeralPairClient`.

    Not thread-safe.
    """

    __slots__ = {
        "_client_ephem_priv": "Client X25519 ephemeral private key.",
        "_client_ephem_pub": "Client X25519 ephemeral public key.",
        "_client_signing_key": "Client long-term Ed25519 signing key.",
        "_pair_addr": "NNG URL of the server PAIR socket to dial.",
        "_server_verify_key": "Server Ed25519 verify key (pinned).",
    }

    _client_ephem_priv: nacl.public.PrivateKey
    _client_ephem_pub: nacl.public.PublicKey
    _client_signing_key: nacl.signing.SigningKey
    _pair_addr: str
    _server_verify_key: nacl.signing.VerifyKey

    def __init__(
        self,
        pair_addr: str,
        client_signing_key: nacl.signing.SigningKey,
        server_verify_key: nacl.signing.VerifyKey,
        *,
        timeout: float = HANDSHAKE_TIMEOUT_S,
    ) -> None:
        pair = nng.PairSocket()
        super().__init__(pair, timeout=timeout)
        self._pair_addr = pair_addr
        self._client_signing_key = client_signing_key
        self._server_verify_key = server_verify_key
        self._client_ephem_priv, self._client_ephem_pub = generate_ephemeral_keypair()

    # ------------------------------------------------------------------
    # _connect / _aconnect
    # ------------------------------------------------------------------

    @override
    def _connect(self) -> None:
        """Dial, send client hello, complete PAIR handshake (sync).

        Raises:
            nacl.exceptions.BadSignatureError: On any auth failure.
            nng.NngError: On transport error.
        """
        self._pair.add_dialer(self._pair_addr).start(block=True)

        # Send client hello (no sig yet — nothing from server to sign against).
        client_challenge = os.urandom(32)
        self._pair.send(make_client_hello(self._client_ephem_pub, client_challenge).to_json())

        # Receive and verify server auth.
        server_ephem_pub, server_challenge = verify_server_auth(
            HandshakeServerAuth.from_json(self._pair.recv().to_bytes()),
            self._server_verify_key,
            self._client_ephem_pub,
            client_challenge,
        )

        # Derive box.
        self._box = make_session_box(self._client_ephem_priv, server_ephem_pub)

        # Send encrypted client confirm.
        self.send(make_client_confirm(
            server_challenge, client_challenge,
            server_ephem_pub, self._client_ephem_pub,
            self._client_signing_key,
        ).to_json())

    @override
    async def _aconnect(self) -> None:
        """Dial, send client hello, complete PAIR handshake (async).

        Raises:
            nacl.exceptions.BadSignatureError: On any auth failure.
            TimeoutError: If the handshake exceeds :attr:`_timeout` seconds.
            nng.NngError: On transport error.
        """
        self._pair.add_dialer(self._pair_addr).start(block=True)

        async with asyncio.timeout(self._timeout):
            # Send client hello (no sig yet — nothing from server to sign against).
            client_challenge = os.urandom(32)
            await self._pair.asend(
                make_client_hello(self._client_ephem_pub, client_challenge).to_json()
            )

            # Receive and verify server auth.
            server_ephem_pub, server_challenge = verify_server_auth(
                HandshakeServerAuth.from_json((await self._pair.arecv()).to_bytes()),
                self._server_verify_key,
                self._client_ephem_pub,
                client_challenge,
            )

            # Derive box.
            self._box = make_session_box(self._client_ephem_priv, server_ephem_pub)

            # Send encrypted client confirm.
            await self.asend(make_client_confirm(
                server_challenge, client_challenge,
                server_ephem_pub, self._client_ephem_pub,
                self._client_signing_key,
            ).to_json())



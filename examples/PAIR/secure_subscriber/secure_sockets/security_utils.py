"""security_utils.py — shared crypto helpers and protocol messages for the secure rep/req/pair.

This module is intentionally self-contained so it can be copied verbatim into
other projects.  It provides:

* Ed25519 identity keys (``load_signing_key``, ``derive_verify_key``)
* Ephemeral X25519 key exchange (``generate_ephemeral_keypair``,
  ``make_session_box``)
* Three protocol messages covering the REP/REQ + PAIR handshake
  (``HandshakeClientHello``, ``HandshakeServerAuth``, ``HandshakeClientConfirm``)
  with signatures and encoding.

Security properties
-------------------
* **Authentication** — every protocol message carries an Ed25519 signature
  over all meaning-carrying fields, preventing impersonation.
* **Forward secrecy** — session keys derive from ephemeral X25519 key pairs,
  so compromise of long-term signing keys does not expose past sessions.
* **Confidentiality** — the NaCl ``Box`` (X25519 + XSalsa20-Poly1305) encrypts
  and authenticates every PAIR message after the handshake.
* **Replay prevention** — each side contributes a 32-byte random challenge;
  the other side must sign *both* challenges, binding the confirm messages to
  exactly this session.

"""

import base64
import json
import pathlib
from dataclasses import dataclass

import nacl.exceptions
import nacl.public
import nacl.signing


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def load_signing_key(path: str | pathlib.Path) -> nacl.signing.SigningKey:
    """Load an Ed25519 signing key from a hex-encoded seed file.

    Args:
        path: Path to a file containing a 64-character hex string (32-byte seed).

    Returns:
        The ``SigningKey`` for the stored seed.

    Raises:
        ValueError: If the file content is not a valid 32-byte hex seed.
    """
    # Load the hex seed from the file
    seed_hex = pathlib.Path(path).read_text(encoding="ascii").strip()

    # Decode it into a 32-byte seed
    try:
        seed_bytes = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise ValueError(f"Key file {path!r} does not contain a valid hex seed") from exc
    if len(seed_bytes) != 32:
        raise ValueError(f"Expected 32-byte seed in {path!r}, got {len(seed_bytes)}")

    # Create and return the SigningKey
    return nacl.signing.SigningKey(seed_bytes)


def derive_verify_key(signing_key: nacl.signing.SigningKey) -> nacl.signing.VerifyKey:
    """Return the Ed25519 verify key corresponding to *signing_key*.

    Args:
        signing_key: An Ed25519 signing key.

    Returns:
        The corresponding verify (public) key.
    """
    return signing_key.verify_key


# ---------------------------------------------------------------------------
# Ephemeral key exchange
# ---------------------------------------------------------------------------


def generate_ephemeral_keypair() -> tuple[nacl.public.PrivateKey, nacl.public.PublicKey]:
    """Generate a fresh X25519 ephemeral key pair.

    Returns:
        A ``(private_key, public_key)`` tuple.  Both are discarded after the
        session ends, providing forward secrecy.
    """
    priv = nacl.public.PrivateKey.generate()
    return priv, priv.public_key


def make_session_box(
    my_private: nacl.public.PrivateKey,
    their_public: nacl.public.PublicKey,
) -> nacl.public.Box:
    """Derive a NaCl ``Box`` from the X25519 ECDH of both ephemeral keys.

    The resulting ``Box`` provides authenticated encryption
    (XSalsa20-Poly1305) with a random nonce per call to ``encrypt``.

    Args:
        my_private: Our ephemeral X25519 private key.
        their_public: The peer's ephemeral X25519 public key.

    Returns:
        A ``nacl.public.Box`` ready for ``encrypt`` / ``decrypt``.
    """
    return nacl.public.Box(my_private, their_public)


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def b64enc(data: bytes) -> str:
    """Encode *data* as a URL-safe base64 string (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64dec(s: str) -> bytes:
    """Decode a URL-safe base64 string, tolerating missing padding."""
    padding = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


# ---------------------------------------------------------------------------
# Protocol messages
# ---------------------------------------------------------------------------

# Handshakes

@dataclass
class HandshakeClientHello:
    """Step 1 (PAIR C→S): client announces its ephemeral key and challenge.

    No signature — there is nothing from the server to sign against yet.
    Sent as the first PAIR message immediately after dialling.

    Attributes:
        client_ephem_pub_b64: Base64-encoded X25519 ephemeral public key.
        client_challenge_b64: Base64-encoded 32-byte random client challenge.
    """

    client_ephem_pub_b64: str
    client_challenge_b64: str

    def to_json(self) -> bytes:
        """Serialise to JSON bytes."""
        return json.dumps(
            {
                "type": "handshake_client_hello",
                "client_ephem_pub": self.client_ephem_pub_b64,
                "client_challenge": self.client_challenge_b64,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str) -> HandshakeClientHello:
        """Deserialise from JSON bytes or string.

        Args:
            data: Raw JSON bytes or a string.

        Returns:
            A populated ``HandshakeClientHello``.

        Raises:
            ValueError: If required fields are missing or the type tag is wrong.
        """
        obj = json.loads(data)
        if obj.get("type") != "handshake_client_hello":
            raise ValueError(
                f"Expected type 'handshake_client_hello', got {obj.get('type')!r}"
            )
        return cls(
            client_ephem_pub_b64=obj["client_ephem_pub"],
            client_challenge_b64=obj["client_challenge"],
        )


def make_client_hello(
    client_ephem_pub: nacl.public.PublicKey,
    client_challenge: bytes,
) -> HandshakeClientHello:
    """Build a :class:`HandshakeClientHello` for the opening PAIR message.

    Args:
        client_ephem_pub: Client's fresh X25519 ephemeral public key.
        client_challenge: 32-byte random challenge for the server to sign.

    Returns:
        A :class:`HandshakeClientHello` ready to serialise and send.
    """
    return HandshakeClientHello(
        client_ephem_pub_b64=b64enc(bytes(client_ephem_pub)),
        client_challenge_b64=b64enc(client_challenge),
    )

@dataclass
class HandshakeServerAuth:
    """Step 2 (PAIR S→C): server proves its identity and provides session material.

    The signature covers ``client_ephem_pub + client_challenge + server_ephem_pub +
    server_challenge``, binding the response to this exact client hello and
    preventing replay or MITM substitution of the server's ephemeral key.

    Attributes:
        server_ephem_pub_b64: Base64-encoded X25519 ephemeral public key.
        server_challenge_b64: Base64-encoded 32-byte random server challenge.
        signature_b64: Ed25519 signature over the client hello fields plus these.
    """

    server_ephem_pub_b64: str
    server_challenge_b64: str
    signature_b64: str

    def to_json(self) -> bytes:
        """Serialise to JSON bytes."""
        return json.dumps(
            {
                "type": "handshake_server_auth",
                "server_ephem_pub": self.server_ephem_pub_b64,
                "server_challenge": self.server_challenge_b64,
                "signature": self.signature_b64,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str) -> HandshakeServerAuth:
        """Deserialise from JSON bytes or string.

        Args:
            data: Raw JSON bytes or a string.

        Returns:
            A populated ``HandshakeServerAuth``.

        Raises:
            ValueError: If required fields are missing or the type tag is wrong.
        """
        obj = json.loads(data)
        if obj.get("type") != "handshake_server_auth":
            raise ValueError(
                f"Expected type 'handshake_server_auth', got {obj.get('type')!r}"
            )
        return cls(
            server_ephem_pub_b64=obj["server_ephem_pub"],
            server_challenge_b64=obj["server_challenge"],
            signature_b64=obj["signature"],
        )


def make_server_auth(
    client_ephem_pub: nacl.public.PublicKey,
    client_challenge: bytes,
    server_ephem_pub: nacl.public.PublicKey,
    server_challenge: bytes,
    server_signing_key: nacl.signing.SigningKey,
) -> HandshakeServerAuth:
    """Build a :class:`HandshakeServerAuth` responding to a client hello.

    Args:
        client_ephem_pub: Client's X25519 ephemeral public key (from the hello).
        client_challenge: 32-byte random challenge from the client hello.
        server_ephem_pub: Server's fresh X25519 ephemeral public key.
        server_challenge: 32-byte random server challenge.
        server_signing_key: Server's long-term Ed25519 signing key.

    Returns:
        A signed :class:`HandshakeServerAuth`.
    """
    signed_data = (
        bytes(client_ephem_pub)
        + client_challenge
        + bytes(server_ephem_pub)
        + server_challenge
    )
    sig = server_signing_key.sign(signed_data).signature
    return HandshakeServerAuth(
        server_ephem_pub_b64=b64enc(bytes(server_ephem_pub)),
        server_challenge_b64=b64enc(server_challenge),
        signature_b64=b64enc(sig),
    )


def verify_server_auth(
    msg: HandshakeServerAuth,
    server_verify_key: nacl.signing.VerifyKey,
    client_ephem_pub: nacl.public.PublicKey,
    client_challenge: bytes,
) -> tuple[nacl.public.PublicKey, bytes]:
    """Verify a :class:`HandshakeServerAuth` and return session material.

    Args:
        msg: The incoming server auth message.
        server_verify_key: The known (pinned) server Ed25519 verify key.
        client_ephem_pub: Our own ephemeral public key (channel-binding check).
        client_challenge: The 32-byte challenge we sent in the client hello.

    Returns:
        ``(server_ephem_pub, server_challenge)`` if verification passes.

    Raises:
        nacl.exceptions.BadSignatureError: If the server signature is invalid.
    """
    server_ephem_bytes = b64dec(msg.server_ephem_pub_b64)
    server_challenge = b64dec(msg.server_challenge_b64)
    sig_bytes = b64dec(msg.signature_b64)

    signed_data = (
        bytes(client_ephem_pub)
        + client_challenge
        + server_ephem_bytes
        + server_challenge
    )
    server_verify_key.verify(signed_data, sig_bytes)
    return nacl.public.PublicKey(server_ephem_bytes), server_challenge

@dataclass
class HandshakeClientConfirm:
    """Step 3 (PAIR C→S): client proves its identity.

    Sent **encrypted** via the established :class:`~nacl.public.Box`, so the
    client's long-term identity is protected from passive observers.

    The signature covers ``server_challenge + client_challenge + server_ephem_pub +
    client_ephem_pub``, binding the proof to this exact session.

    Attributes:
        signature_b64: Ed25519 signature of the session transcript.
    """

    signature_b64: str

    def to_json(self) -> bytes:
        """Serialise to JSON bytes."""
        return json.dumps(
            {"type": "handshake_client_confirm", "signature": self.signature_b64},
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str) -> HandshakeClientConfirm:
        """Deserialise from JSON bytes or string.

        Args:
            data: Raw JSON bytes or a string.

        Returns:
            A populated ``HandshakeClientConfirm``.

        Raises:
            ValueError: If required fields are missing or the type tag is wrong.
        """
        obj = json.loads(data)
        if obj.get("type") != "handshake_client_confirm":
            raise ValueError(
                f"Expected type 'handshake_client_confirm', got {obj.get('type')!r}"
            )
        return cls(signature_b64=obj["signature"])


def make_client_confirm(
    server_challenge: bytes,
    client_challenge: bytes,
    server_ephem_pub: nacl.public.PublicKey,
    client_ephem_pub: nacl.public.PublicKey,
    client_signing_key: nacl.signing.SigningKey,
) -> HandshakeClientConfirm:
    """Build a :class:`HandshakeClientConfirm` closing the handshake.

    Args:
        server_challenge: 32-byte challenge received in the server auth.
        client_challenge: 32-byte challenge we sent in the client hello.
        server_ephem_pub: Server's X25519 ephemeral public key.
        client_ephem_pub: Our own X25519 ephemeral public key.
        client_signing_key: Client's long-term Ed25519 signing key.

    Returns:
        A signed :class:`HandshakeClientConfirm`.
    """
    signed_data = (
        server_challenge
        + client_challenge
        + bytes(server_ephem_pub)
        + bytes(client_ephem_pub)
    )
    sig = client_signing_key.sign(signed_data).signature
    return HandshakeClientConfirm(signature_b64=b64enc(sig))


def verify_client_confirm(
    msg: HandshakeClientConfirm,
    authorized_vks: frozenset[nacl.signing.VerifyKey],
    server_challenge: bytes,
    client_challenge: bytes,
    server_ephem_pub: nacl.public.PublicKey,
    client_ephem_pub: nacl.public.PublicKey,
) -> None:
    """Verify a :class:`HandshakeClientConfirm`.

    Args:
        msg: The incoming client confirm message.
        authorized_vks: Frozen set of authorized Ed25519 verify keys.
        server_challenge: The 32-byte challenge we sent in the server auth.
        client_challenge: The 32-byte challenge the client sent in its hello.
        server_ephem_pub: Our own X25519 ephemeral public key.
        client_ephem_pub: The client's X25519 ephemeral public key.

    Raises:
        nacl.exceptions.BadSignatureError: If the signature does not verify.
    """
    signed_data = (
        server_challenge
        + client_challenge
        + bytes(server_ephem_pub)
        + bytes(client_ephem_pub)
    )
    sig_bytes = b64dec(msg.signature_b64)
    for vk in authorized_vks:
        try:
            vk.verify(signed_data, sig_bytes)
            return
        except nacl.exceptions.BadSignatureError:
            continue
    raise nacl.exceptions.BadSignatureError("No authorized verify key matched the signature")




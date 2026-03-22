"""
Common utilities for the secure PAIR/REP sockets.
"""


from dataclasses import dataclass
import json
import socket as _socket
from typing import Final, Protocol
import uuid


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Handshake timeout in seconds applied on both sides.
HANDSHAKE_TIMEOUT_S: Final[float] = 5.0

# ---------------------------------------------------------------------------
# Some utils
# ---------------------------------------------------------------------------


@dataclass
class ErrorResponse:
    """Error response from the server over REP (e.g. unauthorized client).

    Attributes:
        reason: Human-readable description of the error.
    """

    reason: str

    def to_json(self) -> bytes:
        """Serialise to JSON bytes."""
        return json.dumps(
            {"type": "error", "reason": self.reason},
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str) -> ErrorResponse:
        """Deserialise from JSON bytes or string.

        Args:
            data: Raw JSON bytes or a string.

        Returns:
            A populated ``ErrorResponse``.
        """
        obj = json.loads(data)
        return cls(reason=obj.get("reason", "unknown error"))

class CipherBox(Protocol):
    """Structural interface for an authenticated encryption box.

    Any object implementing ``encrypt`` and ``decrypt`` with these signatures
    satisfies the protocol — including :class:`nacl.public.Box`.
    """

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return an authenticated ciphertext for *plaintext*."""
        ...

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Authenticate and decrypt *ciphertext*, returning the plaintext.

        Raises:
            nacl.exceptions.CryptoError: If authentication or decryption fails.
        """
        ...

# ---------------------------------------------------------------------------
# Transport address selection
# ---------------------------------------------------------------------------


def pick_pair_addr(transport: str) -> str:
    """Pick a free PAIR address for the given *transport*.

    For ``tcp`` the OS is asked to assign a free port so there is no race
    between selecting a port and binding it (the port is freed for nng
    immediately after probing, so a very brief TOCTOU window exists, but this
    is acceptable for a local demo).

    Args:
        transport: One of ``"tcp"``, ``"ipc"``, or ``"inproc"``.

    Returns:
        A fully-qualified NNG URL string.

    Raises:
        ValueError: If *transport* is not one of the supported values.
    """
    if transport == "tcp":
        # Ask the OS for a free port, then release it for nng.
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return f"tcp://127.0.0.1:{port}"

    if transport == "ipc":
        uid = uuid.uuid4().hex[:8]
        return f"ipc:///tmp/nng-pair-{uid}.ipc"

    if transport == "inproc":
        uid = uuid.uuid4().hex[:8]
        return f"inproc://nng-pair-{uid}"

    raise ValueError(f"Unsupported transport {transport!r}; choose tcp, ipc, or inproc")


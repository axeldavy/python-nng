"""
Specific common utils for the secure subscriber example.
"""

import json
from dataclasses import dataclass
from typing import Final

from secure_sockets.security_utils import b64enc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default REP/REQ address for the subscription-request channel.
DEFAULT_REP_URL: Final[str] = "tcp://127.0.0.1:54325"

# Event emission interval in seconds.
DEFAULT_EVENT_INTERVAL_S: Final[float] = 0.5

# Subscription

@dataclass
class SubscribeRequest:
    """Step 1 (REQ → REP): client requests a subscription to *topic*.

    Attributes:
        topic: Subscription topic identifier (arbitrary string).
    """

    topic: str

    def to_json(self) -> bytes:
        """Serialise to JSON bytes."""
        return json.dumps(
            {
                "type": "subscribe_request",
                "topic": self.topic,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str) -> SubscribeRequest:
        """Deserialise from JSON bytes or string.

        Args:
            data: Raw JSON bytes or a string.

        Returns:
            A populated ``SubscribeRequest``.

        Raises:
            ValueError: If required fields are missing or the type tag is wrong.
        """
        obj = json.loads(data)
        if obj.get("type") != "subscribe_request":
            raise ValueError(f"Expected type 'subscribe_request', got {obj.get('type')!r}")
        return cls(
            topic=obj["topic"]
        )

    @classmethod
    def from_topic(cls, topic: str) -> SubscribeRequest:
        """Convenience constructor from a topic string."""
        return cls(topic=topic)


@dataclass
class SubscribeResponse:
    """
    Step 2 (REP → REQ): server acknowledges subscription and provides PAIR details.

    Attributes:
        pair_addr: NNG URL of the PAIR listener the client should connect to.
        connection_token_b64: Base64-encoded opaque token the client must echo back
            after the PAIR handshake to confirm the subscription was authorised.
    """

    pair_addr: str
    connection_token_b64: str

    def to_json(self) -> bytes:
        """Serialise to JSON bytes."""
        return json.dumps(
            {
                "type": "subscribe_response",
                "pair_addr": self.pair_addr,
                "connection_token": self.connection_token_b64
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str) -> SubscribeResponse:
        """Deserialise from JSON bytes or string.

        Args:
            data: Raw JSON bytes or a string.

        Returns:
            A populated ``SubscribeResponse``.

        Raises:
            ValueError: If required fields are missing or the type tag is wrong.
        """
        obj = json.loads(data)
        if obj.get("type") != "subscribe_response":
            raise ValueError(f"Expected type 'subscribe_response', got {obj.get('type')!r}")
        return cls(
            pair_addr=obj["pair_addr"],
            connection_token_b64=obj["connection_token"]
        )

    @classmethod
    def create(cls, pair_addr: str, connection_token: bytes) -> SubscribeResponse:
        """Convenience constructor from raw details."""
        return cls(
            pair_addr=pair_addr,
            connection_token_b64=b64enc(connection_token)
        )



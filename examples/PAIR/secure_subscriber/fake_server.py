#!/usr/bin/env python3
"""Rogue server — demonstrates client-side rejection of an impostor.

This script binds to the **same** REP address as the legitimate server.
It knows the legitimate client's verify key and will accept the subscription
request without complaint — it even generates a PAIR address and starts
listening on it.

However, its subscribe response is signed with the *fake_server* signing key,
not the real server key.  When the legitimate client verifies the response
against the known ``SERVER_VERIFY_KEY``, the signature check fails and the
client aborts before ever connecting to the PAIR socket.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import override

import nacl.signing

from common import DEFAULT_REP_URL
from secure_sockets.security_utils import derive_verify_key, load_signing_key
from secure_sockets.secure_rep import SecureRepServer

# ---------------------------------------------------------------------------
# Configuration — loads the *fake* server key but lets the real client connect
# ---------------------------------------------------------------------------

_HERE = pathlib.Path(__file__).parent
_FAKE_SERVER_KEY_PATH = _HERE / "keys" / "fake_server_sign.key"

#: The rogue server *does* know the legitimate client's verify key so it
#: passes the authorization check — the rejection happens on the client side
#: when verifying the response signature.
AUTHORIZED_CLIENT_KEYS: frozenset[nacl.signing.VerifyKey] = frozenset([
    # Accept the real client
    nacl.signing.VerifyKey(bytes.fromhex(
        "bebe88ef6e5bd5979834368d85481483c78a13107167c4d440b97325535e7064"
    )),
    # Accept the fake client
    nacl.signing.VerifyKey(bytes.fromhex(
        "621d87476029430aaae9b466425db8f86d991d569cd82f8614507776ca1bd471"
    ))
])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("fake_server")

class FakeRepServer(SecureRepServer):
    """Rogue server that accepts subscription requests but signs responses with
    the wrong key, causing the client to reject them."""

    @override
    async def conversation(self, conv) -> None:
        _LOG.info("New conversation started (successful handshake)")

async def main() -> None:
    """Run the rogue server.

    Args:
        transport: NNG transport for PAIR sockets.
    """
    fake_signing_key = load_signing_key(_FAKE_SERVER_KEY_PATH)
    fake_vk_hex = derive_verify_key(fake_signing_key).encode().hex()
    _LOG.info("Rogue server fake verify_key = %s", fake_vk_hex)
    _LOG.info("Rogue server bound to %s — waiting for victims", DEFAULT_REP_URL)

    rep = FakeRepServer(
        server_signing_key=fake_signing_key,
        authorized_vks=AUTHORIZED_CLIENT_KEYS,
    )

    async with asyncio.TaskGroup() as tg:
        rep.serve(tg, DEFAULT_REP_URL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nRogue server stopped.")

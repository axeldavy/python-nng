#!/usr/bin/env python3
"""Fake (unauthorized) client — demonstrates server-side rejection.

This script is identical to ``client.py`` except it loads the *fake_client*
signing key, which is **not** in the server's ``AUTHORIZED_CLIENT_KEYS`` list.

Expected outcome
----------------
The server will refuse the subscription and send back an error response.
This script will log the rejection and exit cleanly with a non-zero status.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

import nacl.signing

from common import DEFAULT_REP_URL
from secure_sockets.security_utils import load_signing_key
from secure_sockets.secure_req import SecureReqClient

# ---------------------------------------------------------------------------
# Configuration — loads the *fake* client key
# ---------------------------------------------------------------------------

_HERE = pathlib.Path(__file__).parent
_FAKE_CLIENT_KEY_PATH = _HERE / "keys" / "fake_client_sign.key"

#: The legitimate server's verify key — fake client trusts it (still connects
#: to the real server to show the rejection path).
SERVER_VERIFY_KEY = (
    nacl.signing.VerifyKey(bytes.fromhex(
    "979c768f74094eb2ca38abc866375c3399b325a9c85cb787c7155e52317b239f"
    ))
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("fake_client")


async def main() -> None:
    """Attempt to subscribe with an unauthorized key, expect rejection."""

    _LOG.info("Fake client starting")
    _LOG.info("Connecting to server at %s", DEFAULT_REP_URL)

    try:
        async with SecureReqClient(
            rep_addr=DEFAULT_REP_URL,
            client_signing_key=load_signing_key(_FAKE_CLIENT_KEY_PATH),
            server_verify_key=SERVER_VERIFY_KEY
        ):
            _LOG.info("Connected to server at %s", DEFAULT_REP_URL)
    except Exception as e:
        _LOG.error("Subscription rejected: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")

#!/usr/bin/env python3
"""Generate Ed25519 signing key pairs for the subscriber example.

Writes hex-encoded 32-byte seeds to the keys/ directory.
Run this once before using server.py / client.py:

    python gen_keys.py

Re-running will overwrite existing keys.  After running, copy the printed
public-key hex values into server.py's AUTHORIZED_CLIENT_KEYS list.
"""

from __future__ import annotations

import pathlib
from nacl.signing import SigningKey

_KEYS_DIR = pathlib.Path(__file__).parent / "keys"
_PARTIES = ["server", "client", "fake_server", "fake_client"]


def generate_all() -> None:
    """Generate and persist Ed25519 key seeds for all demo parties."""
    _KEYS_DIR.mkdir(exist_ok=True)

    print("Generated keys (hex-encoded Ed25519 seeds):\n")
    for name in _PARTIES:
        # Generate a fresh signing key.
        sk = SigningKey.generate()
        seed_hex = sk.encode().hex()
        vk_hex = sk.verify_key.encode().hex()

        # Write the seed to disk.
        key_path = _KEYS_DIR / f"{name}_sign.key"
        key_path.write_text(seed_hex + "\n", encoding="ascii")

        print(f"  {name:<15}  verify_key = {vk_hex}")

    print(
        "\nPaste the 'client' verify_key hex into server.py's AUTHORIZED_CLIENT_KEYS.\n"
        "Paste the 'server' verify_key hex into client.py's SERVER_VERIFY_KEY.\n"
        "Paste the 'server' verify_key hex into fake_client.py's SERVER_VERIFY_KEY.\n"
        "Paste the 'fake_server' verify_key hex into fake_server.py (auto-loaded).\n"
        "Paste the 'client' verify_key hex into fake_server.py's AUTHORIZED_CLIENT_KEYS.\n"
    )


if __name__ == "__main__":
    generate_all()

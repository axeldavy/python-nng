#!/usr/bin/env python3
"""REP server subprocess for the multiprocess REQ-REP demo.

Each REP server listens on a single address, receives request strings from any
number of connected REQ clients, and sends an acknowledgement back.  The server
runs until either its socket is closed (SIGTERM) or its receive timeout fires
(meaning all clients have finished and disconnected).

Invoked by main.py — not intended for direct use.

    python main.py          # spawns servers and clients automatically
"""

from __future__ import annotations

import argparse
import logging
import os

import nng

LOG_FMT = "%(asctime)s.%(msecs)03d  %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def _log(tag: str, msg: str) -> None:
    logging.info("[pid=%-7d  %-16s] %s", os.getpid(), tag, msg)


def run(server_id: int, url: str) -> None:
    tag = f"SERVER-{server_id}"
    _log(tag, f"starting — listening on {url}")

    with nng.RepSocket() as rep:
        # Backstop timeout: if no request arrives within 30 s the clients have
        # all finished; exit cleanly rather than waiting forever.
        rep.recv_timeout = 30_000

        lst = rep.add_listener(url)
        lst.start()
        _log(tag, "listener bound — ready for requests")

        count = 0
        while True:
            try:
                data = rep.recv()
            except nng.NngClosed:
                _log(tag, "socket closed — shutting down")
                break
            except nng.NngTimeout:
                _log(tag, "recv timed out — no more clients, shutting down")
                break

            count += 1
            request = data.decode()
            _log(tag, f"recv #{count:>3d}  ← {request!r}")

            reply = f"ack from server-{server_id}: [{request}]"
            rep.send(reply.encode())
            _log(tag, f"sent #{count:>3d}  → {reply!r}")

    _log(tag, f"done — served {count} request(s)")


# ── Entry point ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--id", type=int, default=0, help="Server index (for logging)")
    p.add_argument("--url", required=True, help="Address to listen on")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt=LOG_DATEFMT)
    run(args.id, args.url)

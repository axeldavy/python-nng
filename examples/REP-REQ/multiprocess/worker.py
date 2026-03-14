#!/usr/bin/env python3
"""REQ client subprocess for the multiprocess REQ-REP demo.

Each REQ client dials *all* server addresses and sends a fixed number of
requests.  nng's REQ protocol automatically routes each outgoing request to
whichever server pipe is ready first, distributing the load across all servers
without any application-level routing.

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


def run(client_id: int, urls: list[str], num_requests: int) -> None:
    tag = f"client-{client_id}"
    _log(tag, f"starting — dialing {len(urls)} server(s), sending {num_requests} request(s)")

    with nng.ReqSocket() as req:
        req.recv_timeout = 30_000
        req.send_timeout = 10_000

        # Dial every server.  block=True waits until each TCP/IPC handshake
        # completes so all pipes are live before the first send.
        for i, url in enumerate(urls):
            _log(tag, f"connecting to {url} …")
            d = req.add_dialer(url)
            d.start(block=True)
            _log(tag, f"pipe-{i} up  ({url})")

        _log(tag, f"all {len(urls)} pipe(s) ready — sending {num_requests} request(s)")

        for k in range(num_requests):
            msg = f"client-{client_id}/req-{k}"
            _log(tag, f"send [{k + 1}/{num_requests}]  → {msg!r}")
            req.send(msg.encode())

            # nng guarantees the reply arrives on the same pipe the request was
            # sent on, so we receive unconditionally here.
            reply = req.recv().decode()
            _log(tag, f"recv [{k + 1}/{num_requests}]  ← {reply!r}")

    _log(tag, f"done — sent {num_requests} request(s)")


# ── Entry point ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--id", type=int, default=0, help="Client index (for logging)")
    p.add_argument("--url", action="append", dest="urls", default=[],
                   metavar="URL", help="Server URL (repeat for each server)")
    p.add_argument("--requests", type=int, default=5, metavar="K",
                   help="Number of requests to send (default: 5)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if not args.urls:
        raise SystemExit("At least one --url is required")
    logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt=LOG_DATEFMT)
    run(args.id, args.urls, args.requests)

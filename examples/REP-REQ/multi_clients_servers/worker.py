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
    """Helper for consistent log formatting across servers and clients."""
    logging.info("[pid=%-7d  %-16s] %s", os.getpid(), tag, msg)


def run(client_id: int, urls: list[str], num_requests: int) -> None:
    """Start a REQ client that dials multiple servers and sends requests."""
    # Log the client's configuration at startup.
    tag = f"client-{client_id}"
    _log(tag, f"starting — dialing {len(urls)} server(s), sending {num_requests} request(s)")

    with nng.ReqSocket() as req:
        # Set timeouts to avoid hanging indefinitely if a server disappears.
        req.recv_timeout = 30_000
        req.send_timeout = 10_000

        # Dial every server.  block=True waits until each TCP/IPC handshake
        # completes so all pipes are live before the first send.
        for i, url in enumerate(urls):
            _log(tag, f"connecting to {url} …")
            d = req.add_dialer(url)
            d.start(block=True)
            _log(tag, f"pipe-{i} up  ({url})")

        # At this point all pipes are connected and ready.
        _log(tag, f"all {len(urls)} pipe(s) ready — sending {num_requests} request(s)")

        # Send num_requests messages, automatically load-balancing across all servers.
        for k in range(num_requests):
            # Prepare the request message and send it.
            msg = f"client-{client_id}/req-{k}"
            _log(tag, f"send [{k + 1}/{num_requests}]  => '{msg}'")
            req.send(msg)

            # nng guarantees the reply arrives on the same pipe the request was
            # sent on, so we receive unconditionally here.
            reply = req.recv()
            _log(tag, f"recv [{k + 1}/{num_requests}]  <= '{reply}'")

    _log(tag, f"done — sent {num_requests} request(s)")


# ── Entry point ────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", type=int, default=0, help="Client index (for logging)")
    parser.add_argument("--url", action="append", dest="urls", default=[],
                   metavar="URL", help="Server URL (repeat for each server)")
    parser.add_argument("--requests", type=int, default=5, metavar="K",
                   help="Number of requests to send (default: 5)")
    args = parser.parse_args()

    # Check that at least one server URL was provided
    if not args.urls:
        raise SystemExit("At least one --url is required")

    # Configure logging to include timestamps and process IDs
    logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt=LOG_DATEFMT)

    # Run the client with the specified ID, server URLs, and number of requests.
    run(args.id, args.urls, args.requests)

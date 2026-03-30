#!/usr/bin/env python3
"""Service subprocess for the RESPONDENT/SURVEYOR service discovery example.

Each service connects to a central surveyor URL and responds to incoming
surveys.  Survey handling runs in a background daemon thread via
:class:`~_responder.ServiceResponder`, leaving the main service loop free.

Usage::

    python service.py --name <name>

Recognised survey commands:
    ``IDENTITY``  – reply with the service name and status.
    ``TERMINATE`` – trigger a clean shutdown of this service instance.

Run ``start_services.py`` instead of invoking this file directly.
"""

import argparse
import time
from typing import Final

from _responder import ServiceResponder 

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SURVEYOR_URL: Final[str] = "tcp://127.0.0.1:54400"

_TICK_INTERVAL_S: Final[float] = 0.1
# Print a heartbeat every N ticks (= every N * _TICK_INTERVAL_S seconds)
_HEARTBEAT_TICKS: Final[int] = 50


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments, start the responder, and run the main service loop."""
    parser = argparse.ArgumentParser(description="Discovery service subprocess")
    parser.add_argument("--name", required=True, help="Unique service name")
    args = parser.parse_args()

    # Start the background survey responder — the main thread stays free
    responder = ServiceResponder(
        _SURVEYOR_URL,
        args.name
    )
    responder.start()
    print(f"[{args.name}] Started — connecting to {_SURVEYOR_URL}", flush=True)

    # Main service loop — simulates real service work running independently
    # of survey handling.  The responder tasks answer surveys concurrently
    # while this loop counts ticks.  A TERMINATE survey raises KeyboardInterrupt
    # in this thread via _thread.interrupt_main().
    tick = 0
    try:
        while True:
            time.sleep(_TICK_INTERVAL_S)
            tick += 1
            if tick % _HEARTBEAT_TICKS == 0:
                elapsed = tick * _TICK_INTERVAL_S
                print(f"[{args.name}] Running... ({elapsed:.0f}s elapsed)", flush=True)
    except KeyboardInterrupt:
        print(f"[{args.name}] Interrupted.", flush=True)


if __name__ == "__main__":
    main()

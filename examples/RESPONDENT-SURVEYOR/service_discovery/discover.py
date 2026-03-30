#!/usr/bin/env python3
"""Service discovery surveyor — queries running services via SURVEYOR/RESPONDENT.

Binds to a well-known address, sends a single survey to all connected services,
and collects every response that arrives within the survey window.

Start services first with ``start_services.py``, then run this script.

Usage::

    python discover.py [--terminate]

Examples::

    # Ask all services to identify themselves (default)
    python discover.py

    # Terminate all services
    python discover.py --terminate
"""

import argparse
import time
from typing import Final

import nng

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_URL: Final[str] = "tcp://127.0.0.1:54400"
# How long the surveyor waits to collect responses (milliseconds)
_DEFAULT_SURVEY_TIME_MS: Final[int] = 500
# How long to wait after binding before sending the survey, to allow services
# that were reconnecting to complete the handshake (milliseconds)
_DEFAULT_WAIT_MS: Final[int] = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _collect_responses(surveyor: nng.SurveyorSocket) -> list[nng.Message]:
    """Drain all responses from *surveyor* until the survey window expires.

    Args:
        surveyor: A :class:`~nng.SurveyorSocket` that has already sent a
            survey via :meth:`~nng.Socket.send`.

    Returns:
        List of decoded response strings, one per responding service.
    """
    responses: list[nng.Message] = []
    while True:
        try:
            msg = surveyor.recv()
            responses.append(msg)
        except nng.NngTimeout:
            # Survey window closed — no more responses will arrive
            break
    return responses

def _get_peer_addr(msg: nng.Message) -> str:
    """Return the peer address from which *msg* was received."""
    pipe = msg.pipe
    if pipe is None:
        return "<unknown>"
    return f"<{pipe.get_peer_addr()}>"

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments, send a survey, and print each response."""
    parser = argparse.ArgumentParser(description="Service discovery surveyor")
    parser.add_argument(
        "--terminate",
        action="store_true",
        help="Send TERMINATE instead of IDENTITY (shuts down all services).",
    )
    args = parser.parse_args()

    wire_request = "TERMINATE" if args.terminate else "IDENTITY"

    with nng.SurveyorSocket() as surveyor:
        # Configure how long nng waits for responses before closing the window
        surveyor.survey_time = _DEFAULT_SURVEY_TIME_MS

        # Bind so that services can connect / reconnect to us
        surveyor.add_listener(_DEFAULT_URL).start()
        print(f"Listening on {_DEFAULT_URL}  (survey_time={_DEFAULT_SURVEY_TIME_MS} ms)")

        # Allow services that were waiting for the surveyor to reconnect
        print(
            f"Waiting {_DEFAULT_WAIT_MS} ms for services to connect...",
            end=" ",
            flush=True,
        )
        time.sleep(_DEFAULT_WAIT_MS / 1000.0)
        print("done.")

        # Broadcast the survey to every connected service
        print(f"\nSurvey: {wire_request!r}")
        surveyor.send(wire_request)

        # Collect all responses that arrive within the survey window
        responses = _collect_responses(surveyor)

    # Report results after the socket is closed
    separator = "─" * 52
    print(f"\n{separator}")
    print(f"Received {len(responses)} response(s):")
    if responses:
        for resp in responses:
            print(f"  {_get_peer_addr(resp)}: {resp}")
    else:
        print("  (no services responded — is start_services.py running?)")
    print(separator)


if __name__ == "__main__":
    main()

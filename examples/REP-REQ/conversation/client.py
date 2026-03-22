#!/usr/bin/env python3
"""Conversation-pattern demo client — drives the CalculatorServer in server.py.

Each client session maintains its own server-side accumulator.  This demo
launches several concurrent sessions concurrently to show that sessions are
fully independent, even when the server handles them over the same socket.

Session A: a simple linear sequence (add, mul, sub, result).
Session B: error-recovery demonstration (bad input, divide by zero, reset).
Session C: a longer workload to show sessions interleaving with A and B.

Usage::

    python client.py [--url tcp://127.0.0.1:5555]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import nng

DEFAULT_URL: str = "tcp://127.0.0.1:5555"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("calc-client")


# ---------------------------------------------------------------------------
# Low-level helper
# ---------------------------------------------------------------------------


async def _exchange(sock: nng.ReqSocket, command: str) -> str:
    """Send *command* and return the server's UTF-8 reply.

    Args:
        sock: An open, connected :class:`nng.ReqSocket`.
        command: Plain-text command to send (e.g. ``"add 7"``).

    Returns:
        The server's reply as a decoded string.
    """
    await sock.asend(command.encode("utf-8"))
    reply: bytes = bytes(await sock.arecv())
    return reply.decode("utf-8")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


async def run_session(
    url: str,
    name: str,
    commands: list[str],
    *,
    step_delay: float = 0.0,
) -> None:
    """Open one REQ connection, execute *commands* in order, then quit.

    Each send/receive pair is one round-trip in the server-side conversation.
    Because a single :class:`nng.ReqSocket` keeps one pipe alive for its
    lifetime, the server sees all messages from this function as belonging to
    the same conversation and therefore the same accumulator.

    Args:
        url: NNG server URL.
        name: Session label used in log output.
        commands: Commands to send, excluding the final ``"quit"``.
        step_delay: Optional sleep between steps to interleave with other sessions.
    """
    with nng.ReqSocket() as sock:
        sock.add_dialer(url).start()

        _LOG.info("[%s] connected", name)

        # Minimal handshake
        await sock.asend(b"start conversation")
        reply = await sock.arecv()
        if reply.to_bytes() != b"ok":
            _LOG.error("[%s] handshake failed: %s", name, reply.to_bytes())
            return

        for cmd in commands:
            if step_delay:
                await asyncio.sleep(step_delay)
            reply = await _exchange(sock, cmd)
            _LOG.info("[%s]  %-14s  →  %s", name, cmd, reply)

        # End the conversation cleanly.
        farewell = await _exchange(sock, "quit")
        _LOG.info("[%s]  quit           →  %s", name, farewell)

    _LOG.info("[%s] disconnected", name)


# ---------------------------------------------------------------------------
# Demo sessions
# ---------------------------------------------------------------------------


async def main(url: str) -> None:
    """Run three concurrent calculator sessions against the server.

    Session A — straightforward arithmetic chain.
    Session B — error recovery: bad token, division by zero, reset.
    Session C — longer workload interleaved with A and B.

    Args:
        url: NNG server URL to connect to.
    """
    async with asyncio.TaskGroup() as tg:
        # Session A: simple linear sequence.
        tg.create_task(
            run_session(
                url,
                name="A",
                commands=[
                    "add 10",    # 10
                    "mul 3",     # 30
                    "sub 5",     # 25
                    "div 5",     # 5
                    "result",    # 5  (no mutation)
                ],
                step_delay=0.04,
            ),
            name="session-A",
        )

        # Session B: error-handling paths, then reset and continue.
        tg.create_task(
            run_session(
                url,
                name="B",
                commands=[
                    "add 100",        # 100
                    "div 0",          # Error: division by zero  (acc unchanged)
                    "add banana",     # Error: not a number       (acc unchanged)
                    "bogus",          # Error: unknown command    (acc unchanged)
                    "result",         # 100
                    "reset",          # 0
                    "add 7",          # 7
                    "mul 6",          # 42
                ],
                step_delay=0.02,
            ),
            name="session-B",
        )

        # Session C: longer workload so sessions visibly interleave.
        tg.create_task(
            run_session(
                url,
                name="C",
                commands=[
                    "add 1",    # 1
                    "add 2",    # 3
                    "add 3",    # 6
                    "add 4",    # 10
                    "add 5",    # 15
                    "mul 2",    # 30
                    "sub 3",    # 27
                    "div 3",    # 9
                    "result",   # 9
                ],
                step_delay=0.01,
            ),
            name="session-C",
        )


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Concurrent conversation-pattern demo client for CalculatorServer."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"NNG server URL (default: {DEFAULT_URL})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    _args = _parse_args()
    try:
        asyncio.run(main(_args.url))
    except* nng.NngError as eg:
        for exc in eg.exceptions:
            _LOG.error("NNG error: %s", exc)

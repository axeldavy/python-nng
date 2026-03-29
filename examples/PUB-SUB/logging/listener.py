#!/usr/bin/env python3
"""PUB-SUB logging demo — subscriber / log listener.

Listens for any running publisher (process.py) and prints each received log
record, optionally filtered to specific levels.

Usage::

    python listener.py [LEVEL ...]

    python listener.py                        # default: WARNING ERROR CRITICAL
    python listener.py DEBUG                  # all levels (DEBUG and above)
    python listener.py ERROR CRITICAL         # errors only

The process runs until interrupted with Ctrl+C.

"""

import json
import logging
import sys

import nng

# ── Constants ─────────────────────────────────────────────────────────────────

URL: str = "tcp://127.0.0.1:54322"
"""nng address of the publisher to bind to."""

_TOPIC_SEP: bytes = b"\n"
"""Separator between topic prefix and JSON payload (must match process.py)."""

_ALL_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
"""Canonical log level names in ascending severity order."""


# ── Listener ──────────────────────────────────────────────────────────────────


class NngLogListener:
    """Receives nng-published log records and dispatches them to a local handler.

    Each nng message is expected to have the form::

        <LEVELNAME>\\n<json-record>

    The payload is decoded from JSON and reconstructed into a
    :class:`logging.LogRecord` via :func:`logging.makeLogRecord`.  The record
    is then passed to *handler* for local rendering.

    Args:
        url: nng address of the publisher to connect to.
        levels: Level names to subscribe to.  An empty list or ``[""]``
            subscribes to all levels.
        handler: Local handler for received records.  Defaults to a
            :class:`logging.StreamHandler` writing to *stderr*.

    Raises:
        ValueError: If *levels* contains an unrecognised level name.
        nng.NngError: If nng cannot bind to *url*.
    """

    def __init__(
        self,
        url: str,
        levels: list[str],
        *,
        handler: logging.Handler | None = None,
    ) -> None:
        """Bind to *url* and register subscriptions for *levels*."""
        for lvl in levels:
            if lvl and lvl not in _ALL_LEVELS:
                raise ValueError(
                    f"Unknown log level {lvl!r}. Must be one of {_ALL_LEVELS}."
                )

        self._handler = handler or logging.StreamHandler()

        sock = nng.SubSocket()

        # Register subscriptions
        if not levels or levels == [""]:
            sock.subscribe(b"")
        else:
            for lvl in levels:
                sock.subscribe(lvl.encode() + _TOPIC_SEP)

        # Start listening for publishers in the background.
        sock.add_listener(url).start()

        self._sock = sock

    def run(self) -> None:
        """Block and dispatch incoming log records until interrupted with Ctrl+C.

        Raises:
            KeyboardInterrupt: Propagated cleanly after the socket is closed.
        """
        print(f"Listening on {URL} … (Ctrl+C to stop)\n", flush=True)
        try:
            while True:
                raw: bytes = self._sock.recv().to_bytes()

                # Split on the first separator to isolate the pickled payload.
                sep_pos = raw.index(_TOPIC_SEP)
                payload = raw[sep_pos + 1:]

                # Decode JSON and reconstruct the LogRecord.
                d: dict[str, object] = json.loads(payload)
                record = logging.makeLogRecord(d)
                self._handler.handle(record)

        except KeyboardInterrupt:
            print("\nListener stopped.", flush=True)
        finally:
            self._sock.close()


# ── Demo ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Parse CLI level arguments and run the listener loop."""
    levels: list[str] = sys.argv[1:] or ["WARNING", "ERROR", "CRITICAL"]

    # Validate before connecting.
    for lvl in levels:
        if lvl not in _ALL_LEVELS:
            print(
                f"error: unknown level {lvl!r}. "
                f"Choices: {', '.join(_ALL_LEVELS)}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Format received records to highlight that they arrived from a remote process.
    fmt = logging.Formatter(
        "[listener] %(levelname)-8s  %(name)-20s — %(message)s"
        "  (%(filename)s:%(lineno)d  pid=%(process)d)"
    )
    local_handler = logging.StreamHandler()
    local_handler.setFormatter(fmt)

    print(f"Subscribing to: {', '.join(levels)}")
    NngLogListener(URL, levels, handler=local_handler).run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PUB-SUB logging demo — publisher / log producer.

Attaches an :class:`NngPubHandler` to the root logger so that log records
flow over an nng PubSocket to any number of remote listeners.

Usage::

    # terminal 1 — start the listener first so it is ready to receive
    python listener.py WARNING ERROR CRITICAL

    # terminal 2
    python process.py

Architecture
------------
:class:`NngPubHandler` serialises each :class:`logging.LogRecord` into::

    <LEVELNAME>\\n<pickled-record-dict>

The ``LEVELNAME`` prefix (e.g. ``WARNING``) is the nng subscription topic.
Remote listeners subscribe to the exact prefix they care about — nng's PUB
socket delivers each message only to matching subscribers with no extra
filtering code required.

The record is serialised with :mod:`json` after pre-rendering any traceback
into ``exc_text`` and resolving ``args`` into ``msg``.  Only the fields
defined in :data:`_RECORD_FIELDS` are transmitted.
"""

import json
import logging
import time

import nng

# ── Constants ─────────────────────────────────────────────────────────────────

URL: str = "tcp://127.0.0.1:54322"
"""nng address the publisher connects to if a listener is available."""

_TOPIC_SEP: bytes = b"\n"
"""Separator between the topic prefix and the JSON payload."""

_RECORD_FIELDS: tuple[str, ...] = (
    "name",
    "levelno",
    "levelname",
    "pathname",
    "filename",
    "module",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "process",
    "processName",
    "msg",
    "exc_text",
    "stack_info",
)
"""LogRecord fields transmitted over the wire."""


# ── Handler ───────────────────────────────────────────────────────────────────


class NngPubHandler(logging.Handler):
    """A :class:`logging.Handler` that publishes records over an nng PubSocket.

    Each emitted record is serialised as::

        <LEVELNAME>\\n<json-record>

    The ``LEVELNAME`` prefix acts as the nng subscription topic, so remote
    subscribers can filter by log level without deserialising the payload.

    Example::

        handler = NngPubHandler("tcp://127.0.0.1:54322")
        handler.setLevel(logging.WARNING)
        logging.getLogger().addHandler(handler)

    Args:
        url: nng address to connect to (e.g. ``"tcp://127.0.0.1:54322"``).

    Raises:
        nng.NngError: If nng cannot connect to *url*.
    """

    def __init__(self, url: str) -> None:
        """Connect an nng PubSocket to *url*."""
        super().__init__()
        self._sock = nng.PubSocket()

        # Connect to any existing process at url, as soon as one is found.
        self._sock.add_dialer(url).start(block=False)

    def emit(self, record: logging.LogRecord) -> None:
        """Serialise and publish *record* to all matching subscribers.

        Pre-renders any traceback into ``exc_text`` and resolves ``args`` into
        ``msg`` before encoding the record as JSON.  Only the fields listed in
        :data:`_RECORD_FIELDS` are transmitted.

        Args:
            record: The log record to publish.
        """
        try:
            # Pre-render traceback so the receiver can display it without
            # needing the original exception class to be installed.
            if record.exc_info:
                self.format(record)

            # Build a JSON-serialisable snapshot using only the known fields.
            d = {
                field: getattr(record, field, None)
                for field in _RECORD_FIELDS
            }
            d["msg"] = record.getMessage()

            # Prefix with the level name as topic, then append the JSON payload.
            topic = record.levelname.encode() + _TOPIC_SEP
            payload = json.dumps(d, default=str).encode()
            self._sock.send(topic + payload)

        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """Close the nng socket and deregister this handler."""
        self._sock.close()
        super().close()


# ── Demo ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the publisher demo: attach the handler and emit sample log records."""
    # Local console echo so we can see what is being published.
    logging.basicConfig(
        level=logging.DEBUG,
        format="[publisher] %(levelname)-8s  %(name)-20s — %(message)s",
    )

    handler = NngPubHandler(URL)
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)

    print(f"Publishing on {URL}")

    # Three loggers simulating different application subsystems.
    db = logging.getLogger("app.database")
    http = logging.getLogger("app.http")
    worker = logging.getLogger("app.worker")

    events: list[tuple[int, logging.Logger, str]] = [
        (logging.DEBUG,    db,     "Query executed in 1.2 ms"),
        (logging.INFO,     http,   "GET /api/v1/health  200 OK"),
        (logging.DEBUG,    worker, "Worker idle, waiting for task"),
        (logging.WARNING,  http,   "Response time exceeded 500 ms threshold"),
        (logging.INFO,     db,     "Connection pool size: 8/10"),
        (logging.ERROR,    worker, "Task failed after 3 retries"),
        (logging.DEBUG,    db,     "Cache miss for key 'user:42'"),
        (logging.WARNING,  db,     "Slow query detected (>100 ms)"),
        (logging.CRITICAL, http,   "Service unreachable: connection refused"),
        (logging.INFO,     worker, "Gracefully shutting down worker-3"),
        (logging.ERROR,    db,     "Deadlock detected, rolling back transaction"),
        (logging.DEBUG,    http,   "Keep-alive ping sent"),
    ]

    for level, logger, msg in events:
        logger.log(level, msg)
        time.sleep(0.1)

    # Brief pause to let nng flush outgoing messages before the socket closes.
    time.sleep(0.3)
    handler.close()
    print("\nPublisher done.")


if __name__ == "__main__":
    main()

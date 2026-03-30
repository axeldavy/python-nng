#!/usr/bin/env python3
"""Start N service subprocesses for the service discovery demo.

Run this script first; each spawned service connects to the well-known
surveyor URL and waits for surveys.  Once the services are running, open a
second terminal and use ``discover.py`` to query them.

Usage::

    python start_services.py [--count N]

Press Ctrl+C to stop all services.

Example session::

    # Terminal 1 — start three services
    python start_services.py --count 3

    # Terminal 2 — query their identities
    python discover.py
    python discover.py --terminate
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_COUNT: Final[int] = 3
_POLL_INTERVAL_S: Final[float] = 1.0
_STOP_TIMEOUT_S: Final[float] = 5.0
_SERVICE_SCRIPT: Final[Path] = Path(__file__).parent / "service.py"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments, spawn service subprocesses, and wait for Ctrl+C."""
    parser = argparse.ArgumentParser(
        description="Start service discovery subprocesses",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=_DEFAULT_COUNT,
        help=f"Number of services to start (default: {_DEFAULT_COUNT})",
    )
    args = parser.parse_args()

    # Spawn one subprocess per service
    procs: list[subprocess.Popen[bytes]] = []
    for i in range(1, args.count + 1):
        name = f"service-{i}"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(_SERVICE_SCRIPT),
                "--name", name,
            ],
        )
        procs.append(proc)
        print(f"Started {name!r}  (pid={proc.pid})")

    print(
        f"\n{args.count} service(s) running.  "
        f"Run discover.py in another terminal to query them.\n"
        f"Press Ctrl+C to stop all.\n"
    )

    # Main watch loop — monitor for exits
    try:
        while True:
            time.sleep(_POLL_INTERVAL_S)
            remaining_procs = procs
            for i, proc in enumerate(procs, 1):
                if proc.poll() is not None:
                    if proc.returncode == 0:
                        print(f"service-{i} exited normally.")
                    else:
                        print(
                            f"[WARNING] service-{i} exited unexpectedly "
                            f"(code={proc.returncode})"
                        )
                    remaining_procs.remove(proc)
            proc = remaining_procs
            if not proc:
                print("All services have exited.")
                return
    except KeyboardInterrupt:
        print("\nStopping all services...")

    # Graceful shutdown — SIGTERM first, SIGKILL on timeout
    for proc in procs:
        proc.terminate()

    for i, proc in enumerate(procs, 1):
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
            print(f"service-{i} stopped  (exit code {proc.returncode})")
        except subprocess.TimeoutExpired:
            print(f"service-{i} did not stop in time — killing.")
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    main()

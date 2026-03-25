#!/usr/bin/env python3
"""REQ-REP multiprocess fan-out demo.
=====================================

Demonstrates how multiple REQ clients automatically load-balance across
multiple REP servers using nng's built-in pipe routing — no application-level
routing code required.

Spawns:
  M  REP server subprocesses  — each listens on its own address.
  N  REQ client subprocesses  — each dials ALL M servers and sends K requests.

nng distributes each outgoing request to whichever server pipe is ready first,
spreading the load without any manual routing logic.

Total messages exchanged: N x K  (spread across M servers).

Usage
-----
    python main.py [--servers M] [--clients N] [--requests K]
                   [--transport {ipc,tcp}] [--base-port PORT]

Defaults: M=3 servers, N=4 clients, K=5 requests → 20 messages total.
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time

LOG_FMT = "%(asctime)s.%(msecs)03d  %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def _log(tag: str, msg: str) -> None:
    logging.info("[pid=%-7d  %-16s] %s", os.getpid(), tag, msg)


def _server_urls(M: int, transport: str, base_port: int) -> list[str]:
    """Build one URL per REP server."""
    if transport == "ipc":
        tmpdir = tempfile.mkdtemp(prefix="nng-demo-")
        return [f"ipc://{tmpdir}/server-{i}.ipc" for i in range(M)]
    return [f"tcp://127.0.0.1:{base_port + i}" for i in range(M)]


def run(M: int, N: int, K: int, transport: str, base_port: int) -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt=LOG_DATEFMT)

    urls = _server_urls(M, transport, base_port)
    here = os.path.dirname(os.path.abspath(__file__))
    py = sys.executable

    _log("MAIN", "=" * 60)
    _log("MAIN", "REQ-REP multiprocess demo")
    _log("MAIN", f"  M = {M} REP server(s)")
    _log("MAIN", f"  N = {N} REQ client(s)")
    _log("MAIN", f"  K = {K} request(s) per client")
    _log("MAIN", f"  Total messages: {N * K}")
    _log("MAIN", f"  Transport: {transport}")
    _log("MAIN", "=" * 60)

    # ── 1. Start REP servers ───────────────────────────────────────────────
    _log("MAIN", f"--- [1] Spawning {M} REP server(s) ---")
    server_procs: list[subprocess.Popen[bytes]] = []
    for i, url in enumerate(urls):
        cmd = [py, os.path.join(here, "server.py"), "--id", str(i), "--url", url]
        p = subprocess.Popen(cmd)
        _log("MAIN", f"  server-{i}  pid={p.pid}  {url}")
        server_procs.append(p)

    # Give each server time to bind its listener before clients start dialing.
    _log("MAIN", "waiting 1 s for servers to bind …")
    time.sleep(1.0)

    # ── 2. Start REQ clients ───────────────────────────────────────────────
    _log("MAIN", f"--- [2] Spawning {N} REQ client(s) ---")

    url_flags: list[str] = []
    for u in urls:
        url_flags += ["--url", u]

    client_procs: list[subprocess.Popen[bytes]] = []
    for i in range(N):
        cmd = [
            py, os.path.join(here, "client.py"),
            "--id", str(i),
            *url_flags,
            "--requests", str(K),
        ]
        p = subprocess.Popen(cmd)
        _log("MAIN", f"  client-{i}  pid={p.pid}")
        client_procs.append(p)

    # ── 3. Wait for clients to finish ──────────────────────────────────────
    _log("MAIN", f"--- [3] Waiting for {N} client(s) ---")
    failed = 0
    for i, p in enumerate(client_procs):
        p.wait()
        status = "OK" if p.returncode == 0 else f"FAILED (rc={p.returncode})"
        _log("MAIN", f"  client-{i} exited — {status}")
        if p.returncode != 0:
            failed += 1

    # ── 4. Shut down servers ───────────────────────────────────────────────
    # Brief pause so servers can finish sending their last replies.
    time.sleep(0.3)
    _log("MAIN", f"--- [4] Terminating {M} server(s) ---")
    for i, p in enumerate(server_procs):
        p.terminate()
        p.wait()
        _log("MAIN", f"  server-{i} terminated  rc={p.returncode}")

    # ── 5. Summary ─────────────────────────────────────────────────────────
    _log("MAIN", "=" * 60)
    if failed == 0:
        _log("MAIN", f"All {N} client(s) completed successfully ✓")
    else:
        _log("MAIN", f"WARNING: {failed}/{N} client(s) failed")
    _log("MAIN", "=" * 60)


# ── Argument parsing ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--servers", "-M", type=int, default=3, metavar="M",
                   help="number of REP server subprocesses (default: 3)")
    p.add_argument("--clients", "-N", type=int, default=4, metavar="N",
                   help="number of REQ client subprocesses (default: 4)")
    p.add_argument("--requests", "-K", type=int, default=5, metavar="K",
                   help="requests each client sends (default: 5)")
    p.add_argument("--transport", choices=["ipc", "tcp"], default="ipc",
                   help="transport layer (default: ipc)")
    p.add_argument("--base-port", type=int, default=54320, metavar="PORT",
                   help="first TCP port when --transport=tcp (default: 54320)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.servers, args.clients, args.requests, args.transport, args.base_port)

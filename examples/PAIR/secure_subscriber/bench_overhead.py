#!/usr/bin/env python3
"""Encryption overhead benchmark for the secure_subscriber demo.

Measures ops/sec for four socket configurations across three transports:

    Plain   REP/REQ   — nng.RepSocket  + nng.ReqSocket
    Secure  REP/REQ   — SecureRepServer + SecureReqClient
    Plain   PAIR/PAIR — nng.PairSocket + nng.PairSocket
    Secure  PAIR/PAIR — SecurePairServer + SecurePairClient

Transports: inproc, ipc, tcp

Prints 12 results (4 configs x 3 transports).

Usage
-----
    python bench_overhead.py
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from typing import override

import nacl.signing

import nng

from secure_sockets.secure_pair import SecurePairClient, SecurePairServer
from secure_sockets.secure_rep import SecureRepServer
from secure_sockets.secure_req import SecureReqClient
from secure_sockets.security_utils import load_signing_key

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HERE = pathlib.Path(__file__).parent

_SERVER_KEY_PATH = _HERE / "keys" / "server_sign.key"
_CLIENT_KEY_PATH = _HERE / "keys" / "client_sign.key"

SERVER_VERIFY_KEY = nacl.signing.VerifyKey(bytes.fromhex(
    "a9517420c135a993b6c91017717e362724c5a6d01b1b127282dfdbd03c74db57"
))
CLIENT_VERIFY_KEY = nacl.signing.VerifyKey(bytes.fromhex(
    "bebe88ef6e5bd5979834368d85481483c78a13107167c4d440b97325535e7064"
))
AUTHORIZED_CLIENT_KEYS: frozenset[nacl.signing.VerifyKey] = frozenset([CLIENT_VERIFY_KEY])

INPROC_URL = "inproc://bench-overhead"
IPC_URL    = "ipc:///tmp/bench-overhead.ipc"
TCP_URL    = "tcp://127.0.0.1:55199"

PAYLOAD    = b"x" * 1024  # tune here the size of the payload
DURATION_S = 3.0
WARMUP_OPS = 200


# ---------------------------------------------------------------------------
# Plain REP/REQ
# ---------------------------------------------------------------------------

async def _bench_plain_reqrep(url: str) -> float:
    """Measure ops/sec for plain RepSocket + ReqSocket."""

    # Server: echo loop in a background task.
    async def _serve(rep: nng.RepSocket, stop: asyncio.Event) -> None:
        with rep:
            while True:
                try:
                    msg = await rep.arecv()
                    await rep.asend(msg.to_bytes())
                except nng.NngClosed:
                    break
                except nng.NngTimeout:
                    pass

    stop = asyncio.Event()
    rep = nng.RepSocket()
    rep.add_listener(url).start()

    serve_task = asyncio.create_task(_serve(rep, stop))

    with nng.ReqSocket() as req:
        try:
            req.add_dialer(url).start(block=True)

            # Warmup
            for _ in range(WARMUP_OPS):
                await req.asend(PAYLOAD)
                await req.arecv()

            # Measure
            count = 0
            deadline = time.perf_counter() + DURATION_S
            while time.perf_counter() < deadline:
                await req.asend(PAYLOAD)
                await req.arecv()
                count += 1
        finally:
            rep.close()
            await serve_task

    return count / DURATION_S


# ---------------------------------------------------------------------------
# Secure REP/REQ
# ---------------------------------------------------------------------------

class _EchoRepServer(SecureRepServer):
    """Echo server: send back every received message."""

    @override
    async def conversation(self, conv) -> None:  # type: ignore[override]
        while True:
            data = await conv.arecv()
            await conv.asend(data)


async def _bench_secure_reqrep(url: str) -> float:
    """Measure ops/sec for SecureRepServer + SecureReqClient."""

    server_signing_key = load_signing_key(_SERVER_KEY_PATH)
    client_signing_key = load_signing_key(_CLIENT_KEY_PATH)

    async with asyncio.TaskGroup() as tg:
        server = _EchoRepServer(server_signing_key, AUTHORIZED_CLIENT_KEYS)
        server.serve(tg, url)

        async with SecureReqClient(
            rep_addr=url,
            client_signing_key=client_signing_key,
            server_verify_key=SERVER_VERIFY_KEY,
        ) as client:
            # Warmup
            for _ in range(WARMUP_OPS):
                await client.asend(PAYLOAD)
                await client.arecv()

            # Measure
            count = 0
            deadline = time.perf_counter() + DURATION_S
            while time.perf_counter() < deadline:
                await client.asend(PAYLOAD)
                await client.arecv()
                count += 1

        server.close()

    return count / DURATION_S


# ---------------------------------------------------------------------------
# Plain PAIR/PAIR
# ---------------------------------------------------------------------------

async def _bench_plain_pair(url: str) -> float:
    """Measure ops/sec for plain PairSocket + PairSocket."""

    async def _serve(srv: nng.PairSocket, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                msg = await srv.arecv()
                await srv.asend(msg.to_bytes())
            except nng.NngClosed:
                break
            except nng.NngTimeout:
                pass

    stop = asyncio.Event()
    srv = nng.PairSocket()
    srv.recv_timeout = 100
    srv.add_listener(url).start()

    serve_task = asyncio.create_task(_serve(srv, stop))

    try:
        cli = nng.PairSocket()
        cli.add_dialer(url).start(block=True)

        # Warmup
        for _ in range(WARMUP_OPS):
            await cli.asend(PAYLOAD)
            await cli.arecv()

        # Measure
        count = 0
        deadline = time.perf_counter() + DURATION_S
        while time.perf_counter() < deadline:
            await cli.asend(PAYLOAD)
            await cli.arecv()
            count += 1

        cli.close()
    finally:
        stop.set()
        srv.close()
        await serve_task

    return count / DURATION_S


# ---------------------------------------------------------------------------
# Secure PAIR/PAIR
# ---------------------------------------------------------------------------

async def _bench_secure_pair(url: str) -> float:
    """Measure ops/sec for SecurePairServer + SecurePairClient."""

    server_signing_key = load_signing_key(_SERVER_KEY_PATH)
    client_signing_key = load_signing_key(_CLIENT_KEY_PATH)

    server = SecurePairServer.create(AUTHORIZED_CLIENT_KEYS, server_signing_key, url)

    async def _serve() -> None:
        async with server as conn:
            try:
                while True:
                    data = await conn.arecv()
                    await conn.asend(data)
            except nng.NngClosed:
                pass

    serve_task = asyncio.create_task(_serve())

    async with SecurePairClient(
        url,
        client_signing_key=client_signing_key,
        server_verify_key=SERVER_VERIFY_KEY,
    ) as client:
        # Warmup
        for _ in range(WARMUP_OPS):
            await client.asend(PAYLOAD)
            await client.arecv()

        # Measure
        count = 0
        deadline = time.perf_counter() + DURATION_S
        while time.perf_counter() < deadline:
            await client.asend(PAYLOAD)
            await client.arecv()
            count += 1

    server.close()
    await serve_task

    return count / DURATION_S


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run_all() -> None:
    """Run all 12 benchmark configurations and print results."""

    configs: list[tuple[str, str, str, object]] = [
        ("plain  REP/REQ", "inproc", INPROC_URL, _bench_plain_reqrep),
        ("plain  REP/REQ", "ipc",    IPC_URL,    _bench_plain_reqrep),
        ("plain  REP/REQ", "tcp",    TCP_URL,    _bench_plain_reqrep),
        ("secure REP/REQ", "inproc", INPROC_URL, _bench_secure_reqrep),
        ("secure REP/REQ", "ipc",    IPC_URL,    _bench_secure_reqrep),
        ("secure REP/REQ", "tcp",    TCP_URL,    _bench_secure_reqrep),
        ("plain  PAIR    ", "inproc", INPROC_URL, _bench_plain_pair),
        ("plain  PAIR    ", "ipc",    IPC_URL,    _bench_plain_pair),
        ("plain  PAIR    ", "tcp",    TCP_URL,    _bench_plain_pair),
        ("secure PAIR    ", "inproc", INPROC_URL, _bench_secure_pair),
        ("secure PAIR    ", "ipc",    IPC_URL,    _bench_secure_pair),
        ("secure PAIR    ", "tcp",    TCP_URL,    _bench_secure_pair),
    ]

    print(f"\n{'Config':<22}  {'Transport':<8}  {'ops/sec':>10}")
    print("-" * 46)

    for label, transport, url, bench_fn in configs:
        ops = await bench_fn(url)  # type: ignore[operator]
        print(f"{label:<22}  {transport:<8}  {ops:>10,.0f}")


if __name__ == "__main__":
    asyncio.run(_run_all())

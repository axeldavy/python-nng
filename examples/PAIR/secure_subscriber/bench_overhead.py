#!/usr/bin/env python3
"""Encryption overhead benchmark for the secure_subscriber demo.

Measures ops/sec for six socket configurations across three transports:

    Plain      REP/REQ   — nng.RepSocket  + nng.ReqSocket
    libsodium  REP/REQ   — SecureRepServer + SecureReqClient
    TLS mTLS   REP/REQ   — TlsConfig.for_server / for_client (TLS 1.3, mTLS)
    Plain      PAIR/PAIR — nng.PairSocket + nng.PairSocket
    libsodium  PAIR/PAIR — SecurePairServer + SecurePairClient
    TLS mTLS   PAIR/PAIR — TlsConfig.for_server / for_client (TLS 1.3, mTLS)

Transports: inproc, ipc, tcp  (TLS only supports tcp — inproc/ipc rows skipped)

Prints up to 18 results (6 configs x 3 transports, TLS skips inproc/ipc).
When nng was compiled without a TLS engine, or when the ``cryptography``
package is absent, TLS rows are omitted with a note.

Payload size governs what the benchmark stresses:

* **Small** (e.g. 64 B):  per-message and protocol overhead dominate.
  Latency, asyncio context-switch cost, and nng framing overhead are visible.
* **Large** (e.g. 1 MB):  bulk symmetric-cipher throughput dominates.
  Differences between AES-NI accelerated vs. software cipher paths are visible.

Usage
-----
    python bench_overhead.py                    # default 1 KiB payload
    python bench_overhead.py --payload-size 64  # 64-byte payload (protocol overhead)
    python bench_overhead.py --payload-size 1M  # 1 MiB payload  (cipher throughput)

Size suffixes: B, K/KB, M/MB, G/GB (case-insensitive).  A bare integer is bytes.
"""

import argparse
import asyncio
import datetime
import ipaddress
import pathlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import override

import nacl.signing

import nng
from nng import TlsConfig, TLS_VERSION_1_3, TLS_AUTH_REQUIRED

from secure_sockets.secure_pair import SecurePairClient, SecurePairServer
from secure_sockets.secure_rep import SecureRepServer
from secure_sockets.secure_req import SecureReqClient
from secure_sockets.security_utils import load_signing_key

# ---------------------------------------------------------------------------
# Common configuration constants
# ---------------------------------------------------------------------------


INPROC_URL = "inproc://bench-overhead"
IPC_URL    = "ipc:///tmp/bench-overhead.ipc"
TCP_URL    = "tcp://127.0.0.1:55199"

# PAYLOAD is set at startup by _parse_args(); default 1 KiB.
PAYLOAD: bytes = b"x" * 1024
DURATION_S = 3.0
WARMUP_OPS = 200

# ---------------------------------------------------------------------------
# TLS availability checks
# ---------------------------------------------------------------------------

_TLS_AVAILABLE: bool = nng.tls_engine_name() != "none"

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        EllipticCurvePrivateKey,
        generate_private_key,
    )
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

_TLS_BENCH_ENABLED: bool = _TLS_AVAILABLE and _HAS_CRYPTOGRAPHY


# ---------------------------------------------------------------------------
# Ephemeral PKI (generated once at module load when TLS is available)
# ---------------------------------------------------------------------------


def _build_pki() -> "dict[str, str]":
    """Build an ephemeral CA + server cert/key + client cert/key for mTLS."""

    def _new_ec_key() -> "EllipticCurvePrivateKey":
        """Return a fresh EC P-256 private key."""
        return generate_private_key(SECP256R1())  # type: ignore[name-defined]

    def _now() -> datetime.datetime:
        """Return the current UTC-aware datetime."""
        return datetime.datetime.now(datetime.timezone.utc)

    # CA
    ca_key = _new_ec_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bench CA")])  # type: ignore[name-defined]
    ca_cert = (
        x509.CertificateBuilder()  # type: ignore[name-defined]
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(_now() - datetime.timedelta(seconds=1))
        .not_valid_after(_now() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)  # type: ignore[name-defined]
        .sign(ca_key, hashes.SHA256())  # type: ignore[name-defined]
    )

    # Server leaf cert (CN=localhost, SAN: DNS:localhost + IP:127.0.0.1)
    srv_key = _new_ec_key()
    srv_cert = (
        x509.CertificateBuilder()  # type: ignore[name-defined]
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))  # type: ignore[name-defined]
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(2)
        .not_valid_before(_now() - datetime.timedelta(seconds=1))
        .not_valid_after(_now() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)  # type: ignore[name-defined]
        .add_extension(
            x509.SubjectAlternativeName([  # type: ignore[name-defined]
                x509.DNSName("localhost"),  # type: ignore[name-defined]
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),  # type: ignore[name-defined]
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())  # type: ignore[name-defined]
    )

    # Client leaf cert (for mTLS)
    cli_key = _new_ec_key()
    cli_cert = (
        x509.CertificateBuilder()  # type: ignore[name-defined]
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bench-client")]))  # type: ignore[name-defined]
        .issuer_name(ca_cert.subject)
        .public_key(cli_key.public_key())
        .serial_number(3)
        .not_valid_before(_now() - datetime.timedelta(seconds=1))
        .not_valid_after(_now() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)  # type: ignore[name-defined]
        .sign(ca_key, hashes.SHA256())  # type: ignore[name-defined]
    )

    def _key_pem(key: "EllipticCurvePrivateKey") -> str:
        """Serialize *key* as an unencrypted PEM string."""
        return key.private_bytes(  # type: ignore[union-attr]
            serialization.Encoding.PEM,  # type: ignore[name-defined]
            serialization.PrivateFormat.TraditionalOpenSSL,  # type: ignore[name-defined]
            serialization.NoEncryption(),  # type: ignore[name-defined]
        ).decode("utf-8")


    def _cert_pem(cert: "x509.Certificate") -> str:
        """Serialize *cert* as a PEM string."""
        return cert.public_bytes(  # type: ignore[union-attr]
            serialization.Encoding.PEM  # type: ignore[name-defined]
        ).decode("utf-8")


    return {
        "ca_pem":       _cert_pem(ca_cert),
        "srv_cert_pem": _cert_pem(srv_cert),
        "srv_key_pem":  _key_pem(srv_key),
        "cli_cert_pem": _cert_pem(cli_cert),
        "cli_key_pem":  _key_pem(cli_key),
    }


# Build the PKI once at import time (only when TLS benchmarks are enabled).
_PKI: "dict[str, str]" = _build_pki() if _TLS_BENCH_ENABLED else {}

# ---------------------------------------------------------------------------
# SecurePair keys
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


# ---------------------------------------------------------------------------
# Shared benchmark helpers
# ---------------------------------------------------------------------------

async def _echo_until_closed(sock: nng.Socket) -> None:
    """Receive messages and echo them back until the socket is closed."""
    while True:
        try:
            msg = await sock.arecv()
            await sock.asend(msg.to_bytes())
        except nng.NngClosed:
            break
        except nng.NngTimeout:
            pass


async def _measure(
    send: Callable[[bytes], Awaitable[None]],
    recv: Callable[[], Awaitable[object]],
) -> float:
    """Warm up, then measure ops/sec for a send/recv pair.

    Sends PAYLOAD and waits for a reply for WARMUP_OPS rounds (not timed),
    then repeats for DURATION_S seconds and returns the measured ops/sec.

    Args:
        send: Async callable that sends a bytes payload to the peer.
        recv: Async callable that receives the echo reply.

    Returns:
        Measured operations per second over the timed window.
    """
    # Warmup: let the connection and any lazy allocations settle.
    for _ in range(WARMUP_OPS):
        await send(PAYLOAD)
        await recv()

    # Timed measurement window.
    count = 0
    deadline = time.perf_counter() + DURATION_S
    while time.perf_counter() < deadline:
        await send(PAYLOAD)
        await recv()
        count += 1

    return count / DURATION_S


def _make_tls_configs() -> "tuple[TlsConfig, TlsConfig]":
    """Return (server_cfg, client_cfg) for mTLS using the ephemeral PKI."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=_PKI["srv_cert_pem"],
        key_pem=_PKI["srv_key_pem"],
        ca_pem=_PKI["ca_pem"],
        auth_mode=TLS_AUTH_REQUIRED,
        min_version=TLS_VERSION_1_3,
    )
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=_PKI["ca_pem"],
        cert_pem=_PKI["cli_cert_pem"],
        key_pem=_PKI["cli_key_pem"],
        min_version=TLS_VERSION_1_3,
    )
    return srv_cfg, cli_cfg


# ---------------------------------------------------------------------------
# Plain REP/REQ
# ---------------------------------------------------------------------------

async def _bench_plain_reqrep(url: str) -> float:
    """Measure ops/sec for plain RepSocket + ReqSocket."""
    rep = nng.RepSocket()
    rep.add_listener(url).start()
    serve_task = asyncio.create_task(_echo_until_closed(rep))

    with nng.ReqSocket() as req:
        try:
            req.add_dialer(url).start(block=True)
            return await _measure(req.asend, req.arecv)
        finally:
            rep.close()
            await serve_task


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
            result = await _measure(client.asend, client.arecv)

        server.close()

    return result


# ---------------------------------------------------------------------------
# Plain PAIR/PAIR
# ---------------------------------------------------------------------------

async def _bench_plain_pair(url: str) -> float:
    """Measure ops/sec for plain PairSocket + PairSocket."""
    srv = nng.PairSocket()
    srv.recv_timeout = 100
    srv.add_listener(url).start()
    serve_task = asyncio.create_task(_echo_until_closed(srv))

    try:
        cli = nng.PairSocket()
        cli.add_dialer(url).start(block=True)
        try:
            return await _measure(cli.asend, cli.arecv)
        finally:
            cli.close()
    finally:
        srv.close()
        await serve_task


# ---------------------------------------------------------------------------
# Secure PAIR/PAIR
# ---------------------------------------------------------------------------

async def _bench_secure_pair(url: str) -> float:
    """Measure ops/sec for SecurePairServer + SecurePairClient."""
    server_signing_key = load_signing_key(_SERVER_KEY_PATH)
    client_signing_key = load_signing_key(_CLIENT_KEY_PATH)

    # SecurePairServer uses a context manager to accept one connection,
    # so it cannot reuse _echo_until_closed (which expects an nng.Socket).
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
        result = await _measure(client.asend, client.arecv)

    server.close()
    await serve_task
    return result


# ---------------------------------------------------------------------------
# TLS mTLS REP/REQ
# ---------------------------------------------------------------------------

async def _bench_tls_reqrep(url: str) -> float:
    """Measure ops/sec for mTLS RepSocket + ReqSocket (TLS 1.3, mutual auth)."""
    srv_cfg, cli_cfg = _make_tls_configs()

    # Bind to an ephemeral port so concurrent bench runs don't collide.
    rep = nng.RepSocket()
    rep.recv_timeout = int(DURATION_S * 2 * 1000)
    lst = rep.add_listener(url, tls=srv_cfg)
    lst.start()
    serve_task = asyncio.create_task(_echo_until_closed(rep))

    with nng.ReqSocket() as req:
        try:
            req.recv_timeout = int(DURATION_S * 2 * 1000)
            req.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start(block=True)
            return await _measure(req.asend, req.arecv)
        finally:
            rep.close()
            await serve_task


# ---------------------------------------------------------------------------
# TLS mTLS PAIR/PAIR
# ---------------------------------------------------------------------------

async def _bench_tls_pair(url: str) -> float:
    """Measure ops/sec for mTLS PairSocket + PairSocket (TLS 1.3, mutual auth)."""
    srv_cfg, cli_cfg = _make_tls_configs()

    srv = nng.PairSocket()
    srv.recv_timeout = int(DURATION_S * 2 * 1000)
    lst = srv.add_listener(url, tls=srv_cfg)
    lst.start()
    serve_task = asyncio.create_task(_echo_until_closed(srv))

    try:
        cli = nng.PairSocket()
        cli.recv_timeout = int(DURATION_S * 2 * 1000)
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start(block=True)
        try:
            return await _measure(cli.asend, cli.arecv)
        finally:
            cli.close()
    finally:
        srv.close()
        await serve_task

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def _run_all() -> None:
    """Run all benchmark configurations and print results."""

    # TLS benches pass "tls+tcp://127.0.0.1:0" as a bind URL; the bench
    # function internally queries lst.port and dials the ephemeral port.
    _TLS_BIND = "tls+tcp://127.0.0.1:0"

    # (label, transport, url, bench_fn, enabled)
    configs: list[tuple[str, str, str, object, bool]] = [
        ("plain      REP/REQ", "inproc", INPROC_URL, _bench_plain_reqrep,  True),
        ("plain      REP/REQ", "ipc",    IPC_URL,    _bench_plain_reqrep,  True),
        ("plain      REP/REQ", "tcp",    TCP_URL,    _bench_plain_reqrep,  True),
        ("libsodium  REP/REQ", "inproc", INPROC_URL, _bench_secure_reqrep, True),
        ("libsodium  REP/REQ", "ipc",    IPC_URL,    _bench_secure_reqrep, True),
        ("libsodium  REP/REQ", "tcp",    TCP_URL,    _bench_secure_reqrep, True),
        ("TLS mTLS   REP/REQ", "tcp",    _TLS_BIND,  _bench_tls_reqrep,   _TLS_BENCH_ENABLED),
        ("plain      PAIR    ", "inproc", INPROC_URL, _bench_plain_pair,   True),
        ("plain      PAIR    ", "ipc",    IPC_URL,    _bench_plain_pair,   True),
        ("plain      PAIR    ", "tcp",    TCP_URL,    _bench_plain_pair,   True),
        ("libsodium  PAIR    ", "inproc", INPROC_URL, _bench_secure_pair,  True),
        ("libsodium  PAIR    ", "ipc",    IPC_URL,    _bench_secure_pair,  True),
        ("libsodium  PAIR    ", "tcp",    TCP_URL,    _bench_secure_pair,  True),
        ("TLS mTLS   PAIR    ", "tcp",    _TLS_BIND,  _bench_tls_pair,    _TLS_BENCH_ENABLED),
    ]

    if not _TLS_BENCH_ENABLED:
        if not _TLS_AVAILABLE:
            print("\nNote: TLS rows skipped — nng compiled without TLS engine.")
        else:
            print("\nNote: TLS rows skipped — 'cryptography' package not installed.")

    payload_kb = len(PAYLOAD) / 1024
    size_label = (
        f"{len(PAYLOAD)} B" if len(PAYLOAD) < 1024
        else f"{payload_kb:.4g} KiB" if payload_kb < 1024
        else f"{payload_kb / 1024:.4g} MiB"
    )
    print(f"\nPayload: {size_label}")
    print(f"\n{'Config':<26}  {'Transport':<8}  {'ops/sec':>10}  {'MB/s':>8}")
    print("-" * 62)

    mb_per_op = len(PAYLOAD) / (1024 * 1024)
    for label, transport, url, bench_fn, enabled in configs:
        if not enabled:
            print(f"{label:<26}  {transport:<8}  {'(skipped)':>10}  {'':>8}")
            continue
        ops = await bench_fn(url)  # type: ignore[operator]
        # Each op = one request + one reply, so 2× the payload bytes are transferred.
        mbps = ops * mb_per_op * 2
        print(f"{label:<26}  {transport:<8}  {ops:>10,.0f}  {mbps:>7.1f}")


def _parse_size(value: str) -> int:
    """Parse a human-readable byte size into an integer.

    Accepts a bare integer (bytes) or an integer followed by a
    case-insensitive suffix: B, K, KB, M, MB, G, GB.

    Args:
        value: Size string such as ``"64"``, ``"4K"``, ``"1MB"``.

    Returns:
        The size in bytes.

    Raises:
        argparse.ArgumentTypeError: If the string cannot be parsed.
    """
    match = re.fullmatch(r"(\d+)\s*([BKMG]B?)?$", value.strip(), re.IGNORECASE)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"Invalid size {value!r}. Use e.g. 64, 4K, 1MB."
        )
    amount = int(match.group(1))
    suffix = (match.group(2) or "B").upper().rstrip("B") or "B"
    multipliers: dict[str, int] = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    return amount * multipliers[suffix]


def _parse_args() -> int:
    """Parse command-line arguments and return the payload size in bytes."""
    parser = argparse.ArgumentParser(
        description="Encryption overhead benchmark.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Payload size advice:\n"
            "  Small (e.g. 64B, 1K)  → measures per-message protocol overhead\n"
            "  Large (e.g. 256K, 1M) → measures bulk cipher throughput\n"
        ),
    )
    parser.add_argument(
        "--payload-size",
        metavar="SIZE",
        default="1K",
        type=_parse_size,
        help="Payload size per message (default: 1K). Suffixes: B K KB M MB G GB.",
    )
    return parser.parse_args().payload_size

if __name__ == "__main__":
    PAYLOAD = b"x" * _parse_args()
    asyncio.run(_run_all())

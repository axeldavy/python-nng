#!/usr/bin/env python3
"""Rejected TLS connections demo — handshake failures are silent on the server.
===============================================================================

Demonstrates that failed TLS handshakes are **not** reported via the server's
``on_new_pipe`` callback.  Only a connection that completes the full handshake
— including any certificate-verification step — receives a pipe object and
triggers the callback.

Three scenarios are run in sequence against the same mTLS server:

Scenario A — Unknown CA
    The client builds a completely separate PKI (different CA).  The server
    certificate cannot be validated → a TLS handshake exception is raised on
    ``dialer.start(block=True)``.  ``on_new_pipe`` does *not* fire.

Scenario B — Missing client certificate
    The client trusts the correct CA but presents no certificate of its own.
    The server enforces ``auth_mode=TLS_AUTH_REQUIRED`` and rejects the
    handshake → a TLS handshake exception is raised.  ``on_new_pipe`` does
    *not* fire.

Note on exception types
    The exact exception raised for a rejected handshake depends on the TLS
    engine and the failure mode.  Mbed TLS raises ``NngCryptoError`` for CA
    validation failures (low-level crypto error) and may raise ``NngAuthError``
    for higher-level authentication rejections.  Both are caught below.

Scenario C — Valid mTLS
    The client presents the correct certificate signed by the shared CA.
    The handshake succeeds, ``on_new_pipe`` fires, and a round-trip message
    is exchanged.

Requirements
------------
    pip install cryptography     # for ephemeral certificate generation
    pip install nng-ssl          # or any nng build with TLS support

Usage
-----
    python rejected.py
"""

import threading

import nng
from nng import NngAuthError, NngCryptoError, TLS_AUTH_REQUIRED, TlsConfig

# Both NngAuthError and NngCryptoError can be raised for a rejected TLS
# handshake depending on the engine (mbed, openssl, wolf) and failure mode.
_TLS_HANDSHAKE_ERRORS = (NngAuthError, NngCryptoError)

from _pki import build_pki

BIND_URL = "tls+tcp://127.0.0.1:0"


def main() -> None:
    """Run all three rejection / acceptance scenarios."""
    if nng.tls_engine_name() == "none":
        raise SystemExit(
            "This demo requires nng to be compiled with TLS support.\n"
            "Install the nng-ssl package:  pip install nng-ssl"
        )

    print("=== Rejected TLS connections demo ===\n")
    print(f"TLS engine : {nng.tls_engine_name()}")
    print(f"nng version: {nng.version()}\n")

    # ── Build the canonical PKI used by the server and the valid client ───────
    pki = build_pki()

    # ── Server TlsConfig (mTLS — requires a client certificate) ──────────────
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki.srv_cert_pem,
        key_pem=pki.srv_key_pem,
        ca_pem=pki.ca_pem,
        auth_mode=TLS_AUTH_REQUIRED,
    )

    # ── Single long-lived server socket shared across all scenarios ───────────
    pipe_count = 0

    with nng.RepSocket() as rep:
        # recv_timeout keeps the server from blocking forever between scenarios.
        rep.recv_timeout = 5_000

        def _on_new_pipe(pipe: nng.Pipe) -> None:
            """Increment the counter whenever a pipe is accepted."""
            nonlocal pipe_count
            pipe_count += 1
            print(f"    [server] on_new_pipe fired (pipe #{pipe_count}) — "
                  f"peer addr: {pipe.get_peer_addr()}")

        rep.on_new_pipe = _on_new_pipe

        lst = rep.add_listener(BIND_URL, tls=srv_cfg)
        lst.start()
        actual_url = f"tls+tcp://127.0.0.1:{lst.port}"
        print(f"Server listening on {actual_url}\n")

        # ── Scenario A — Unknown CA ───────────────────────────────────────────
        print("── Scenario A: client uses an unknown CA ──────────────────────")
        print("   Expected: TLS handshake error on dialer.start(); on_new_pipe NOT called\n")

        # Build a *second*, completely independent PKI — different CA.
        foreign_pki = build_pki()
        foreign_cli_cfg = TlsConfig.for_client(
            server_name="localhost",
            ca_pem=foreign_pki.ca_pem,            # wrong CA — will not validate server cert
            cert_pem=foreign_pki.cli_cert_pem,
            key_pem=foreign_pki.cli_key_pem,
        )

        pipes_before = pipe_count
        with nng.ReqSocket() as req_a:
            try:
                req_a.add_dialer(actual_url, tls=foreign_cli_cfg).start(block=True)
                print("    [client] ERROR — expected a TLS error but handshake succeeded!")
            except _TLS_HANDSHAKE_ERRORS as exc:
                print(f"    [client] {type(exc).__name__} raised as expected: {exc}")

        if pipe_count == pipes_before:
            print("    [server] on_new_pipe was NOT called — connection correctly rejected\n")
        else:
            print(f"    [server] ERROR — pipe_count changed to {pipe_count} unexpectedly\n")

        # ── Scenario B — No client certificate ───────────────────────────────
        print("── Scenario B: client presents no certificate ─────────────────")
        print("   Expected: TLS handshake error on dialer.start(); on_new_pipe NOT called\n")

        no_cert_cli_cfg = TlsConfig.for_client(
            server_name="localhost",
            ca_pem=pki.ca_pem,                    # correct CA (server cert IS trusted)
            # cert_pem and key_pem intentionally omitted — no client cert
        )

        pipes_before = pipe_count
        with nng.ReqSocket() as req_b:
            try:
                req_b.add_dialer(actual_url, tls=no_cert_cli_cfg).start(block=True)
                print("    [client] ERROR — expected a TLS error but handshake succeeded!")
            except _TLS_HANDSHAKE_ERRORS as exc:
                print(f"    [client] {type(exc).__name__} raised as expected: {exc}")

        if pipe_count == pipes_before:
            print("    [server] on_new_pipe was NOT called — connection correctly rejected\n")
        else:
            print(f"    [server] ERROR — pipe_count changed to {pipe_count} unexpectedly\n")

        # ── Scenario C — Valid mTLS ───────────────────────────────────────────
        print("── Scenario C: valid mTLS client ──────────────────────────────")
        print("   Expected: handshake succeeds, on_new_pipe fires, round-trip works\n")

        valid_cli_cfg = TlsConfig.for_client(
            server_name="localhost",
            ca_pem=pki.ca_pem,
            cert_pem=pki.cli_cert_pem,
            key_pem=pki.cli_key_pem,
        )

        # The server needs to serve one request — run it in a background thread.
        pipe_accepted = threading.Event()
        _orig_on_new_pipe = rep.on_new_pipe

        def _on_new_pipe_c(pipe: nng.Pipe) -> None:
            """Delegate to the original counter callback and signal the event."""
            _orig_on_new_pipe(pipe)
            pipe_accepted.set()

        rep.on_new_pipe = _on_new_pipe_c

        def _serve_one() -> None:
            """Receive one request and send an echo reply."""
            try:
                request = rep.recv()
                print(f"    [server] recv <= '{request}'")
                rep.send(f"echo: {request}")
            except nng.NngTimeout:
                print("    [server] recv timed out (scenario C server thread)")

        server_thread = threading.Thread(target=_serve_one, daemon=True)
        server_thread.start()

        with nng.ReqSocket() as req_c:
            req_c.add_dialer(actual_url, tls=valid_cli_cfg).start(block=True)
            print("    [client] mTLS handshake complete\n")

            msg = "hello from valid client"
            print(f"    [client] send => '{msg}'")
            req_c.send(msg)

            reply = req_c.recv()
            print(f"    [client] recv <= '{reply}'")

        server_thread.join(timeout=5)
        if pipe_accepted.wait(timeout=2):
            print("\n    [server] on_new_pipe WAS called — valid pipe accepted")
        else:
            print("\n    [server] ERROR — on_new_pipe was not called for valid client")

    print("\n=== Rejected TLS connections demo complete ===")
    print(f"    Total pipes accepted across all scenarios: {pipe_count}  (expected: 1)")


if __name__ == "__main__":
    main()

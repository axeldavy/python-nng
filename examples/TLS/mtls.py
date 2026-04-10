#!/usr/bin/env python3
"""Mutual TLS (mTLS) demo — *both* sides present and verify a certificate.
==========================================================================

Shows mTLS: the server presents a certificate *and* explicitly requires the
client to present one too (``auth_mode=TLS_AUTH_REQUIRED`` on the server side).
The client provides its own certificate so the handshake succeeds.

What happens
------------
1. Both server and client are configured with a certificate signed by the same
   CA, plus the CA bundle for verification.
2. The server sets ``auth_mode=TLS_AUTH_REQUIRED`` so it will reject any
   client that does not present a valid certificate.
3. The client provides its own ``cert_pem``/``key_pem``, allowing the
   handshake to succeed.
4. A single REP/REQ round-trip confirms the mutually-authenticated channel.

The ``on_new_pipe`` callback fires only after both sides successfully verify
each other — a rejected handshake would prevent the pipe from being created
(see ``rejected.py`` for that scenario).

Requirements
------------
    pip install cryptography     # for ephemeral certificate generation
    pip install nng-ssl          # or any nng build with TLS support

Usage
-----
    python mtls.py
"""

import threading

import nng
from nng import TLS_AUTH_REQUIRED, TlsConfig

from _pki import build_pki

# ── Ephemeral PKI ─────────────────────────────────────────────────────────────
PKI = build_pki()

BIND_URL = "tls+tcp://127.0.0.1:0"


def main() -> None:
    """Run the mTLS demo."""
    if nng.tls_engine_name() == "none":
        raise SystemExit(
            "This demo requires nng to be compiled with TLS support.\n"
            "Install the nng-ssl package:  pip install nng-ssl"
        )

    print("=== Mutual TLS (mTLS) demo ===\n")
    print(f"TLS engine : {nng.tls_engine_name()}")
    print(f"nng version: {nng.version()}\n")

    # ── Server TlsConfig ──────────────────────────────────────────────────────
    # auth_mode=TLS_AUTH_REQUIRED: the server demands a client certificate.
    # ca_pem is used to verify the client certificate.
    srv_cfg = TlsConfig.for_server(
        cert_pem=PKI.srv_cert_pem,
        key_pem=PKI.srv_key_pem,
        ca_pem=PKI.ca_pem,
        auth_mode=TLS_AUTH_REQUIRED,
    )

    # ── Client TlsConfig ──────────────────────────────────────────────────────
    # The client presents its own certificate (cert_pem + key_pem) so the
    # server's mandatory client-auth check passes.
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=PKI.ca_pem,
        cert_pem=PKI.cli_cert_pem,
        key_pem=PKI.cli_key_pem,
    )

    # ── Server socket ─────────────────────────────────────────────────────────
    pipe_accepted = threading.Event()

    with nng.RepSocket() as rep:
        def _on_new_pipe(pipe: nng.Pipe) -> None:
            """Log each accepted pipe and signal the main thread."""
            print(f"  [server] pipe accepted — peer addr: {pipe.get_peer_addr()}")
            pipe_accepted.set()

        rep.on_new_pipe = _on_new_pipe

        lst = rep.add_listener(BIND_URL, tls=srv_cfg)
        lst.start()
        actual_url = f"tls+tcp://127.0.0.1:{lst.port}"
        print(f"Server listening on {actual_url}\n")

        def _serve() -> None:
            """Receive one request and send an echo reply."""
            request = rep.recv()
            print(f"  [server] recv <= '{request}'")
            rep.send(f"echo: {request}")

        server_thread = threading.Thread(target=_serve, daemon=True)
        server_thread.start()

        # ── Client socket ─────────────────────────────────────────────────────
        with nng.ReqSocket() as req:
            req.add_dialer(actual_url, tls=cli_cfg).start(block=True)
            print("[client] mTLS handshake complete — both sides authenticated\n")

            msg = "hello over mTLS"
            print(f"  [client] send => '{msg}'")
            req.send(msg)

            reply = req.recv()
            print(f"  [client] recv <= '{reply}'")

        server_thread.join(timeout=5)
        if not pipe_accepted.is_set():
            raise AssertionError("on_new_pipe was never called — pipe not accepted")

    print("\n=== mTLS demo complete ===")


if __name__ == "__main__":
    main()

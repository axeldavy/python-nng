#!/usr/bin/env python3
"""One-way TLS demo — only the *server* presents a certificate.
=============================================================

Shows the most common TLS setup: the client verifies the server's identity
(certificate chain validated against the CA), but the server does **not**
require the client to present a certificate.

What happens
------------
1. The server creates a :class:`nng.TlsConfig` with its certificate and
   private key.  ``auth_mode`` defaults to ``TLS_AUTH_NONE`` — no client
   certificate is requested or checked.
2. The client creates a :class:`nng.TlsConfig` carrying only the CA
   certificate it trusts and the expected server name.  ``auth_mode``
   defaults to ``TLS_AUTH_REQUIRED`` — the server cert is rigorously
   verified.
3. A single REP/REQ round-trip is performed to confirm the encrypted channel
   works end-to-end.

The "pipe accepted" log line from ``on_new_pipe`` demonstrates that the
connection is visible as a normal nng pipe once the TLS handshake succeeds.

Requirements
------------
    pip install cryptography     # for ephemeral certificate generation
    pip install nng-ssl          # or any nng build with TLS support

Usage
-----
    python one_way.py
"""

import threading

import nng
from nng import TlsConfig

from _pki import build_pki

# ── Ephemeral PKI (CA + server cert/key + client cert/key) ────────────────────
PKI = build_pki()

# Ephemeral port — OS picks any free port; we read it back after bind.
BIND_URL = "tls+tcp://127.0.0.1:0"


def main() -> None:
    """Run the one-way TLS demo."""
    if nng.tls_engine_name() == "none":
        raise SystemExit(
            "This demo requires nng to be compiled with TLS support.\n"
            "Install the nng-ssl package:  pip install nng-ssl"
        )

    print("=== One-way TLS demo ===\n")
    print(f"TLS engine : {nng.tls_engine_name()}")
    print(f"nng version: {nng.version()}\n")

    # ── Server TlsConfig ──────────────────────────────────────────────────────
    # auth_mode defaults to TLS_AUTH_NONE: the server presents its certificate
    # but does NOT request one from the client.
    srv_cfg = TlsConfig.for_server(
        cert_pem=PKI.srv_cert_pem,
        key_pem=PKI.srv_key_pem,
    )

    # ── Client TlsConfig ──────────────────────────────────────────────────────
    # auth_mode defaults to TLS_AUTH_REQUIRED: the client will reject the
    # connection if the server cert cannot be validated against ca_pem.
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=PKI.ca_pem,
    )

    # ── Server socket ─────────────────────────────────────────────────────────
    pipe_accepted = threading.Event()

    with nng.RepSocket() as rep:
        def _on_new_pipe(pipe: nng.Pipe) -> None:
            """Log each accepted pipe and signal the main thread."""
            print(f"  [server] pipe accepted — peer addr: {pipe.get_peer_addr()}")
            pipe_accepted.set()

        rep.on_new_pipe = _on_new_pipe

        # Bind on an OS-assigned ephemeral port.
        lst = rep.add_listener(BIND_URL, tls=srv_cfg)
        lst.start()
        actual_url = f"tls+tcp://127.0.0.1:{lst.port}"
        print(f"Server listening on {actual_url}\n")

        # Serve a single request in a background thread so the main thread can
        # run the client synchronously below.
        def _serve() -> None:
            """Receive one request and send an echo reply."""
            request = rep.recv()
            print(f"  [server] recv <= '{request}'")
            rep.send(f"echo: {request}")

        server_thread = threading.Thread(target=_serve, daemon=True)
        server_thread.start()

        # ── Client socket ─────────────────────────────────────────────────────
        with nng.ReqSocket() as req:
            # block=True: wait until the TLS handshake completes (or raises
            # NngAuthError if the server certificate cannot be verified).
            req.add_dialer(actual_url, tls=cli_cfg).start(block=True)
            print("[client] TLS handshake complete — connection established\n")

            msg = "hello over TLS"
            print(f"  [client] send => '{msg}'")
            req.send(msg)

            reply = req.recv()
            print(f"  [client] recv <= '{reply}'")

        # Wait for the server thread to finish and confirm the pipe fired.
        server_thread.join(timeout=5)
        if not pipe_accepted.is_set():
            raise AssertionError("on_new_pipe was never called — pipe not accepted")

    print("\n=== one-way TLS demo complete ===")


if __name__ == "__main__":
    main()

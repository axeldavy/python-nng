# Secure Subscriber Demo

A demonstration of a **mutually-authenticated, end-to-end encrypted publish/subscribe
system** built on top of `python-nng`.  The transport is plain NNG (no built-in TLS),
with all security handled in Python via [PyNaCl](https://pynacl.readthedocs.io/).

---

## What this demo shows

| Property | How it is achieved |
|---|---|
| **Server authentication** | Ed25519 signature on session transcript; client pins the server's verify key |
| **Client authentication** | Ed25519 signature on session transcript; server maintains an allowlist of verify keys |
| **Confidentiality** | XSalsa20-Poly1305 authenticated encryption (NaCl `Box`) keyed from ephemeral X25519 DH |
| **Forward secrecy** | Fresh ephemeral X25519 keypair generated per session; long-term keys never leave the key file |
| **Replay / MITM protection** | 32-byte random challenges in both directions, signed and bound to the exchanged ephemeral keys |
| **Subscription authorization** | A one-time connection token ties each PAIR feed back to an authorized REP subscription |


To put it in simpler words: The client opens a secure REP/REQ to the channel and subscribes to feeds.
The server sends back to the client exclusive PAIR addresses for each feed, along with one-time connection tokens.
The client opens a secure PAIR connection to each feed address, providing as well the one-time token to prove that it is the same client that made the subscription request.
The server then sends the events to the client over the PAIR connection.

This system allows the server to only prepare the feeds needed by clients, and by the occasion shows one way to implement a secure handshake and encrypted channel on top of NNG using PyNaCl.

Besides the security aspect, the demo also shows how to write a REP server which maintains an independent stateful conversation with each of its clients (while the basic REP/REQ pattern treats each request as independent and stateless).
---

## Architecture

```
Client                                    Server
  │                                          │
  │  ── REQ (subscription request) ────────► │  ← SecureRepServer
  │                                          │    • verifies Ed25519 client proof
  │  ◄── REP (pair_addr + token) ──────────  │    • spawns EphemeralPairServer task
  │                                          │
  │  ── PAIR dial ──────────────────────────► │  ← EphemeralPairServer
  │       3-way handshake                    │    step 1  C→S   ClientHello      (ephem_pub_C, challenge_C)
  │       ────────────────                   │    step 2  S→C   ServerAuth       (ephem_pub_S, challenge_S, sig_S)
  │                                          │    step 3  C→S   ClientConfirm    (sig_C, encrypted)
  │  ── send connection token (encrypted) ──► │    step 4  C→S   connection token
  │                                          │
  │  ◄── encrypted event stream ──────────── │
```

The **REP/REQ channel** is also secured (see `secure_rep.py` / `secure_req.py`): the
client proves its identity before the server reveals any PAIR address.

The **PAIR channel** uses `nacl.public.Box` (X25519 Diffie-Hellman + XSalsa20-Poly1305)
keyed from the two ephemeral keys exchanged during the handshake.  All subsequent
messages — including the connection token confirmation — are encrypted.

---

## Files

```
secure_subscriber/
├── gen_keys.py              Key generation utility (run once)
├── server.py                Subscription server entry point
├── client.py                Subscription client entry point
├── fake_server.py           Rogue-server test (should be rejected by client)
├── fake_client.py           Unauthorised-client test (should be rejected by server)
├── common.py                Shared dataclasses (SubscribeRequest / SubscribeResponse)
├── bench_overhead.py        Encryption overhead benchmark
├── requirements.txt         Python dependencies (pynacl)
└── secure_sockets/
    ├── security_utils.py    Handshake message types, sign/verify helpers
    ├── protocol_utils.py    CipherBox protocol, transport address picker
    ├── secure_pair.py       SecurePairServer / SecurePairClient base classes
    ├── secure_rep.py        SecureRepServer base class
    └── secure_req.py        SecureReqClient base class
```

---

## Quick start

### 1. Install dependencies

```bash
# From the repo root, in your virtual environment:
pip install -e ".[examples]"
pip install pynacl
```

### 2. Generate keys

```bash
cd examples/PAIR/secure_subscriber
python gen_keys.py
```

This writes four Ed25519 seed files under `keys/` and prints each party's verify key.
Paste the printed hex values into the `AUTHORIZED_CLIENT_KEYS` list in `server.py` and
the `SERVER_VERIFY_KEY` constant in `client.py` as instructed.

### 3. Run the server

```bash
python server.py
# optional flags:
#   --transport  tcp | ipc | inproc   (default: tcp)
#   --interval   SECONDS              (default: 0.5)
```

### 4. Run the client

```bash
python client.py --topics sensors telemetry --max-events 20
```

### 5. Test rejection

```bash
# Rogue server — client must refuse connection:
python fake_server.py

# Unauthorised client — server must refuse subscription:
python fake_client.py
```

---

## Handshake sequence

```
PAIR C→S  ClientHello:
            client_ephem_pub    (X25519, 32 bytes, base64)
            client_challenge    (random 32 bytes, base64)

PAIR S→C  ServerAuth:
            server_ephem_pub    (X25519, 32 bytes, base64)
            server_challenge    (random 32 bytes, base64)
            signature           Ed25519 over (client_ephem_pub ‖ client_challenge
                                              ‖ server_ephem_pub ‖ server_challenge)

            → client derives shared_secret = X25519(client_ephem_priv, server_ephem_pub)
            → client verifies signature against pinned server verify key

PAIR C→S  ClientConfirm:   ← encrypted with shared_secret
            signature       Ed25519 over (server_challenge ‖ client_challenge
                                          ‖ server_ephem_pub ‖ client_ephem_pub)

            → server verifies signature against allowlisted client verify keys

PAIR C→S  connection_token (encrypted) — binds this PAIR session to the REP subscription

          ← encrypted event stream
```

---
l
## Security alternatives

This demo deliberately keeps all security logic in pure Python (no C extensions beyond
PyNaCl itself) to make the protocol easy to audit.  For production use, consider these
alternatives:

### NNG built-in TLS (`nng.TlsConfig`)

NNG has native TLS support, which is not yet exposed in `python-nng`.  When
both sides load X.509 certificates and set `TLS_AUTH_REQUIRED`, NNG performs mutual
TLS at the transport layer automatically — no application-level handshake code is
needed.

**Pros:** zero application code for encryption/auth; standard X.509 PKI tooling; works
with existing certificate infrastructure.  
**Cons:** requires a CA or self-signed certificate management workflow; weaker identity
model (any cert signed by the CA is accepted); no built-in application-level allowlist.

```python
tls = TlsConfig.for_server(
    cert_pem=cert_pem,
    key_pem=key_pem,
    ca_pem=ca_pem,
    auth_mode=TLS_AUTH_REQUIRED,
    min_version=TLS_VERSION_1_3,
)

listener = socket.listen("tls+tcp://0.0.0.0:5555", tls=tls)
```

Note the Pypi package isn't built with TLS support, so the README for TLS support.

### ZeroMQ CURVE

[CurveZMQ](http://curvezmq.org/) is ZeroMQ's built-in authenticated encryption
protocol, also based on X25519 + XSalsa20-Poly1305 (Curve25519 + NaCl).  It is
configured via socket options (`zmq.CURVE_SERVER`, `zmq.CURVE_PUBLICKEY`, etc.).

**Pros:** seamless integration in the ZMQ API; no extra code for the handshake.  
**Cons:** ZMQ-only — not portable to other transports; server-authenticated only by
default (client keys must be explicitly provisioned for mutual auth); the CURVE
protocol is ZMQ-proprietary and not interoperable with TLS.

### Noise Protocol Framework

The [Noise Protocol Framework](https://noiseprotocol.org/) (e.g. via
[`noiseprotocol`](https://github.com/piotr-isaq/noiseprotocol)) provides a family of
formally-specified handshake patterns (XX, IK, XK, …).  Pattern `XX` performs full
mutual authentication with forward secrecy and is close in spirit to what this demo
implements manually.

**Pros:** formally specified and analysed; composable patterns for different trust
models; well-suited to application-layer encryption over any transport.  
**Cons:** requires a Noise library; less widely known than TLS; not natively integrated
with NNG.

### WireGuard / VPN tunnel

For deployments where both peers are under your control, wrapping the NNG connection
inside a [WireGuard](https://www.wireguard.com/) tunnel (or any VPN) offloads all
cryptographic concerns to the network layer.

**Pros:** zero application code changes; strong security (Noise IKpsk2 under the hood);
excellent performance.  
**Cons:** requires infrastructure (WireGuard peers, key distribution); not suitable
when you need fine-grained per-topic or per-client authorization at the application
layer.

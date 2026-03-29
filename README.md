# nng

High-performance Python bindings for [nng](https://nng.nanomsg.org/) (nanomsg-next-gen), written in Cython.

Nanomsg-next-gen (NNG) is a messaging library written in C. It provides a small, consistent API for building distributed applications around a set of well-defined communication patterns: request-reply, publish-subscribe, pipeline, pair, survey, and bus. The library handles message framing, back-pressure, reconnection, and transport selection, so application code can focus on the protocol logic.

## Table of contents

- [Why yet another Python wrapper for NNG?](#why-yet-another-python-wrapper-for-nng)
- [Installation](#installation)
- [Quick start](#quick-start)
  - [REP / REQ](#rep--req)
  - [PUB / SUB](#pub--sub)
  - [PUSH / PULL](#push--pull)
  - [PAIR](#pair)
- [Async support](#async-support)
- [Transports](#transports)
- [Communication patterns](#communication-patterns)
- [TLS support](#tls-support)
- [Performance](#performance)
- [On the use of AI](#on-the-use-of-ai)
- [License](#license)


## Why yet another Python wrapper for NNG?

Two Python wrappers for NNG already existed when this project was started.

My motivation for writing a third one came from practical experience with ZeroMQ. When looking for a first message library to experiment upon, it is quite natural to start with ZeroMQ, which is heavily used. However, what seemed simple on paper, lead to a lot of boilerplate to have things work properly. I was not satisfied and gave up on the topic for some time. After coming back on the topic, I decided to give NNG a try. However, the existing wrappers did not feel as complete as PyZMQ: docstrings were sparse, error handling was inconsistent, and there were no benchmarks to help make informed choices.

A more specific concern was asyncio overhead. The ZeroMQ Python bindings had measurable overhead in async code, which I had partially mitigated with a custom selector backed by `zmq_poll`. NNG's callback-based completion model seemed more amenable to tight asyncio integration. Achieving that level of integration requires Cython, which the other wrappers did not use.

The result is a library with:

- Full type annotations and docstrings on all public symbols.
- A `.pyi` stub file automatically generated from the compiled extension.
- Synchronous send/recv, async (`await`), and thread-safe `concurrent.futures` variants on every socket and context.
- Explicit error hierarchy: every nng error code maps to a typed Python exception.
- Benchmarks of the various send/recv alternatives, with and without encryption.
- Examples covering every communication pattern.

## What I like about NNG

Having used both ZeroMQ and NNG, I found that very quickly when using ZeroMQ you need to use ROUTER sockets everywhere, which frees from contraints, but adds significant complexity that is easy to mess up (handling client failure, heartbeats, etc). On the other hand, NNG provides more handling in its sockets, and more flexibility. Need to handle several clients concurrently ? Just open several contexts on the same socket. The context system is very powerful and light. It was well-designed to keep application logic simple. Another design difference is that NNG is entirely thought to be thread safe. Closing a socket from another thread is safe, and will simply cause pending operations to fail with the exception `NngClosed`. This makes it much easier to write clean shutdown code without needing to worry about synchronization or cancellation. Finally it is worth noticing NNG has built-in TLS support.


## Installation

The package requires Python 3.11 or later and a C++ compiler. NNG is bundled as a submodule and compiled automatically.

```bash
pip install python-nng
```

To build from source:

```bash
git clone --recurse-submodules https://github.com/axeldavy/python-nng.git
cd python-nng
pip install .
```

See [TLS support](#tls-support) for instructions on enabling TLS at build time.


## Quick start

### REP / REQ

The REQ/REP pattern implements synchronous request-reply. The requester sends a message and blocks until a reply arrives. The replier receives one request at a time and must send exactly one reply before receiving the next.

```python
import nng

# Server side (in a thread or separate process)
rep = nng.RepSocket()
rep.add_listener("tcp://127.0.0.1:5555").start()
while True:
    msg = rep.recv()
    rep.send(b"pong")

# Client side
req = nng.ReqSocket()
req.add_dialer("tcp://127.0.0.1:5555").start()
req.send(b"ping")
reply = req.recv()
print(reply)  # "pong"
```

To handle multiple clients concurrently on the server side without threads, open independent contexts. Each context maintains its own request-reply state machine:

```python
import asyncio
import nng

async def handle(ctx: nng.Context) -> None:
    while True:
        msg = await ctx.arecv()
        await ctx.asend(b"pong")

async def main() -> None:
    rep = nng.RepSocket()
    rep.add_listener("tcp://127.0.0.1:5555").start()
    workers = [rep.open_context() for _ in range(8)]
    await asyncio.gather(*(handle(ctx) for ctx in workers))
```

### PUB / SUB

The PUB/SUB pattern distributes messages from one publisher to any number of subscribers. Subscribers receive only messages whose body starts with a registered prefix.

```python
import nng

# Publisher
pub = nng.PubSocket()
pub.add_listener("tcp://127.0.0.1:5556").start()
pub.send(b"news.sports result of the match")
pub.send(b"news.weather it will rain")

# Subscriber
sub = nng.SubSocket()
sub.add_dialer("tcp://127.0.0.1:5556").start()
sub.subscribe(b"news.sports")   # receive only sports messages
# sub.subscribe(b"")            # or subscribe to everything
msg = sub.recv()
```

There is no acknowledgment: if a subscriber cannot consume messages fast enough, the oldest messages in its buffer are dropped by default. The publisher is never blocked by a slow subscriber.

### PUSH / PULL

The PUSH/PULL pattern distributes work items across a pool of workers. Each message goes to exactly one worker, chosen by load-balancing.

```python
import nng

# Work distributor
push = nng.PushSocket()
push.add_listener("tcp://127.0.0.1:5557").start()
for i in range(100):
    push.send(f"task-{i}")

# Worker (run several of these in parallel)
pull = nng.PullSocket()
pull.add_dialer("tcp://127.0.0.1:5557").start()
while True:
    task = pull.recv()
    print("processing", task)
```

### PAIR

The PAIR pattern connects exactly two sockets in a bidirectional channel. It is the simplest pattern: each side can send and receive freely. Once a peer has connected, no other peer can connect (even after a disconnect). This makes it ideal for in-process communication between threads, or for one-to-one protocols between processes.

```python
import nng

srv = nng.PairSocket()
srv.add_listener("inproc://myapp").start()

cli = nng.PairSocket()
cli.add_dialer("inproc://myapp").start()

cli.send(b"hello")
print(srv.recv())  # "hello"
```


## Async support

Every socket and context exposes three ways to send and receive:

| Method | Description |
|--------|-------------|
| `send` / `recv` | Blocking call. Releases the GIL while waiting. |
| `asend` / `arecv` | Coroutines. Use with `await` inside an `asyncio` event loop. |
| `submit_send` / `submit_recv` | Returns a `concurrent.futures.Future`, which resolves upon completion. |

Note that it is possible to mix them, or call them concurrently from multiple threads. In addition it is worth noting that Ctrl-C is fully supported (unlike in some other libraries), even with `send` and `recv`.

```python
import asyncio
import nng

async def main() -> None:
    rep = nng.RepSocket()
    rep.add_listener("tcp://127.0.0.1:5558").start()

    req = nng.ReqSocket()
    req.add_dialer("tcp://127.0.0.1:5558").start()

    await req.asend(b"hello")
    msg = await rep.arecv()
    await rep.asend(msg.to_bytes().upper())
    reply = await req.arecv()
    print(reply)  # "HELLO"

asyncio.run(main())
```

The library also provides as async send/recv variant async generators `arecv_ready` and `asend_ready` which yield once each time the socket transitions from not-ready to ready, but they should be reserved for advanced use.


## Transports

All transports are selected by URL scheme and are otherwise interchangeable from application code.

| Scheme | Description |
|--------|-------------|
| `tcp://host:port` | TCP/IP. Accepts both IPv4 and IPv6. Use `tcp4://` or `tcp6://` to force one version. |
| `ipc:///path` | Unix domain sockets (POSIX) or named pipes (Windows). |
| `inproc://name` | In-process communication within the same Python process. Zero-copy where possible. |
| `abstract://name` | Linux abstract namespace sockets. Not persisted to the filesystem. |
| `tls+tcp://host:port` | TCP with TLS. Requires a TLS-enabled build (see below). |
| `ws://host:port/path` | WebSocket. |
| `wss://host:port/path` | WebSocket over TLS. |

Listeners bind to an address; dialers connect to one. The distinction is orthogonal to the protocol role: a REP socket can dial and a REQ socket can listen.

To bind to an ephemeral TCP port, pass port 0 in the listener URL. The assigned port can be retrieved from `listener.port` after `start()` returns.


## Communication patterns

| Socket pair | Pattern | Use case |
|-------------|---------|----------|
| `ReqSocket` / `RepSocket` | Request-reply | RPC, command-response |
| `PubSocket` / `SubSocket` | Publish-subscribe | Event fan-out, topic feeds |
| `PushSocket` / `PullSocket` | Pipeline | Task queues, stream processing |
| `PairSocket` / `PairSocket` | Pair | Bidirectional point-to-point channel |
| `SurveyorSocket` / `RespondentSocket` | Survey | Voting, service discovery |
| `BusSocket` / `BusSocket` | Bus | All-to-all broadcast mesh |

Each pattern has well-defined send/recv semantics. Violating them — for instance calling `send` twice on a `ReqSocket` without an intervening `recv` — raises `NngState`.


## TLS support

By default, python-nng is built without TLS support. The PyPI package does not include TLS either.

To build with TLS, install one of the three supported backends (mbedTLS, OpenSSL, or wolfSSL) and pass the appropriate cmake options at install time:

```bash
# mbedTLS
pip install . --config-settings "cmake.define.NNG_ENABLE_TLS=ON" --config-settings "cmake.define.NNG_TLS_ENGINE=mbed"

# OpenSSL
pip install . --config-settings "cmake.define.NNG_ENABLE_TLS=ON" --config-settings "cmake.define.NNG_TLS_ENGINE=openssl"

# wolfSSL
pip install . --config-settings "cmake.define.NNG_ENABLE_TLS=ON" --config-settings "cmake.define.NNG_TLS_ENGINE=wolf"
```

Once built, TLS connections use the `tls+tcp://` scheme. Configuration is handled through `TlsConfig`:

```python
import nng

srv_cfg = nng.TlsConfig.for_server(
    cert_pem=open("server.crt").read(),
    key_pem=open("server.key").read(),
    ca_pem=open("ca.crt").read(),       # required for mutual TLS
    auth_mode=nng.TLS_AUTH_REQUIRED,
    min_version=nng.TLS_VERSION_1_3,
)

rep = nng.RepSocket()
lst = rep.add_listener("tls+tcp://0.0.0.0:0", tls=srv_cfg)
lst.start()
print(f"listening on port {lst.port}")
```

Three authentication modes are available:

| Constant | Meaning |
|----------|---------|
| `TLS_AUTH_NONE` | No peer certificate is requested. |
| `TLS_AUTH_OPTIONAL` | A peer certificate is verified if presented. |
| `TLS_AUTH_REQUIRED` | A peer certificate is required (mutual TLS). This is the default for clients. |

TLS 1.2 and 1.3 are supported. The minimum and maximum protocol versions can be set via `min_version` and `max_version` using the `TLS_VERSION_1_2` and `TLS_VERSION_1_3` constants.

A comparison of plain, libsodium-encrypted, and TLS-encrypted throughput at various payload sizes is available in `examples/PAIR/secure_subscriber/bench_overhead.py`.


## Performance

See [PERFORMANCE.md](PERFORMANCE.md) for detailed benchmarks and interpretation.

In brief: on a local machine the async round-trip overhead (excluding cipher cost) is on the order of a few microseconds for inproc, and grows with transport cost for IPC and TCP. TLS throughput depends heavily on whether the TLS backend was compiled with hardware AES acceleration.


## On the use of AI

Much of the writing has been assisted by AI (Claude Sonnet 4.6), in particular the benchmark code and the unit tests. I have 12 years of Python experience, and more in C/C++. I have written another Cython-based library [DearCyGui](https://github.com/DearCyGui/DearCyGui). Thus while AI has been a great help, I have extensively reviewed, extended, and modified the code and documentation, and I am confident that the code is of good quality. If you find any issue, please open an issue or a PR.


## License

python-nng is licensed under the MIT License. See [LICENSE](LICENSE) for more details.

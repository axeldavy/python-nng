# nng

High-performance Python bindings for [nng](https://nng.nanomsg.org/) (nanomsg-next-gen), written in Cython.

Nanomsg-next-gen (NNG) is a messaging library written in C. It provides a small, consistent API for building distributed applications around a set of well-defined communication patterns: request-reply, publish-subscribe, pipeline, pair, survey, and bus. The library handles message framing, back-pressure, reconnection, and transport selection, so application code can focus on the protocol logic.

## Table of contents

- [Why use a library like NNG rather than sockets directly?](#why-use-a-library-like-nng-rather-than-sockets-directly)
- [Why yet another Python wrapper for NNG?](#why-yet-another-python-wrapper-for-nng)
- [What I like about NNG](#what-i-like-about-nng)
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

## Why use a library like NNG rather than sockets directly?

The Python standard library has a lot of available functions for communicating with sockets between processes on the same or different computers.

Sockets are essentially data queues managed by the kernel and used to communicate between processes. When connecting to a website via TCP, or when receiving a video stream via UDP, you use sockets.

With the Python standard library, there are essentially two main ways to use sockets:

- via the `socket` library.
- via `asyncio`.

NNG provides an abstraction on top, enabling shorter and easier-to-read code.
It provides a socket-like interface, except that:
- The socket can listen on or bind to several interfaces at the same time.
- It is possible to send or wait on a message before a peer has connected
- Rather than handling individual connections with connected peers, you use a socket
   type that has implicit handling of peers. For instance a PUB socket will send a copy of any
   message sent to all connected peers.
- Message semantics is directly handled (low level sockets are typically configured to receive a bytestream rather than messages). A background thread handles receiving incoming messages and queuing them (you need to quickly handle the reception of incoming messages to prevent blocking reception if the kernel queue is full).
- Reconnection is handled automatically

Underneath, NNG is similar to a dedicated thread running asyncio. Both use epoll/kqueue/iocp to quickly detect sockets ready for reading/writing. It is programmed in C and fully thread-safe.
Because it uses a dedicated thread, you can get asynchronous I/O without asyncio. `submit_recv` returns a future that resolves when a message is received. However this Python library also integrates NNG to an existing asyncio loop.

Don't use NNG if:
- You want the lowest latency possible. In terms of latency, asyncio NNG (`arecv`) > raw asyncio > NNG (`recv`) > raw socket. If you want below, don't use Python. See [PERFORMANCE.md](PERFORMANCE.md) for detailed benchmarks and interpretation.
- You want to share heavy amount of data. NNG is based on TCP and similar, while for video streaming or remote desktop, UDP with custom handling of lost packets is more appropriate. For inter-process large data sharing, shared memory is preferred over named pipes/unix sockets (which NNG ipc uses), as they avoid kernel copies. If you don't need to maximize performance and can accept some data copies, NNG is appropriate though.
- You need to integrate with another protocol (HTTP, custom, etc)

Use NNG if:
- You are building distributed or multi-process applications and want well-defined message semantics without managing low-level socket details.
- You need multiple communication patterns (request-reply, pub-sub, pipeline, pair, survey, bus) with a consistent, interchangeable API.
- You want automatic reconnection, back-pressure, and message framing out of the box.
- You want to mix blocking calls, `asyncio` coroutines, and `concurrent.futures` within the same codebase.
- You need concurrent request handling on a server without the overhead of one thread per client (use contexts).
- You want the same API for ipc, tcp or inproc endpoints
- You don't want to manage each connection individually
- You don't want to require asyncio to get asynchronous server/client connections, send/recv.

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

Having used both ZeroMQ and NNG, I found that ZeroMQ quickly pushes you toward ROUTER sockets everywhere, which removes constraints but adds significant complexity that is easy to get wrong (client failure handling, heartbeats, etc). NNG, by contrast, provides richer built-in socket behaviour and more flexibility. Need to handle several clients concurrently? Just open several contexts on the same socket. The context system is powerful and lightweight, and was well-designed to keep application logic simple.

Another key difference is that NNG is designed to be fully thread-safe. Closing a socket from another thread is safe and will simply cause pending operations to fail with `NngClosed`, making it much easier to write clean shutdown code without worrying about synchronization or cancellation. NNG also has built-in TLS support.


## Installation

The package requires Python 3.11 or later and a C++ compiler. NNG is bundled as a submodule and compiled automatically.

```bash
pip install nng
```

For TLS support (`tls+tcp://`, `wss://`), install the pre-built package that bundles Mbed TLS:

```bash
pip install nng-ssl
```

The `nng-ssl` wheel is drop-in compatible with `nng`; the same `import nng` works for both.  Install one or the other, not both.

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

The library also provides async generators `arecv_ready` and `asend_ready` for advanced use; each yields once every time the socket transitions from not-ready to ready.


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

By default, `nng` is built without TLS support to keep the package pure MIT.

### Pre-built wheel with TLS

The easiest way to get TLS support is the `nng-ssl` package on PyPI.  It bundles
Mbed TLS 4.1.0 (Apache-2.0 license) compiled as a static library and requires no
extra system dependencies:

```bash
pip install nng-ssl
```

`nng-ssl` installs as the `nng` Python package, so existing code is unchanged.
Do not install both `nng` and `nng-ssl` in the same environment.

### Build from source with Mbed TLS (auto-fetched)

To build `nng-ssl` locally, Mbed TLS is downloaded and compiled automatically
during the cmake configure step (internet access required at build time):

```bash
git clone --recurse-submodules https://github.com/axeldavy/python-nng.git
cd python-nng
cp builtin_tls/pyproject.toml pyproject.toml
pip install .
```

### Build from source with a system TLS library

You can also build `nng` (not `nng-ssl`) against an independently installed TLS
library:

```bash
# Mbed TLS (system-installed)
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

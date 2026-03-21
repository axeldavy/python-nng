## Foreword

Wondering about the performance of python-nng?

NNG's main goal is to simplify the development of high-performance messaging applications.

It provides an API that transparently handles:
- Thread safety
- I/O multiplexing
- Asynchronous I/O
- Message queue semantics
- A message is either received complete, or not at all
- Protocols and transports

Naturally, this adds a bit of overhead, but how much ?

## Benchmarks

Here is below a run of the benchmarks included in this repository.

The benchmarks below are run on an Intel Haswell quad-core Intel(R) Core(TM) i5-4690 CPU @ 3.50GHz.

They are run in the best possible conditions, that is no other running processes, etc.
The OS is Linux 6.18.13-zen with real-time scheduling priority enabled for the benchmark processes.

The Python version is 3.14.0 freethreaded. Benchmarks were run on 21 March 2026 and corresponds to the state of commit 7d9d1daa.

```python
python -m run_benchmarks --sizes 64 --iters 100 --warmup 10 --no-plot
```

### Latency — inproc — 64 B

| Method | min µs | p50 µs | p95 µs | p99 µs | max µs |
|--------|-------:|-------:|-------:|-------:|-------:|
| python_raw | 7.4 | 7.6 | 8.2 | 16.8 | 23.6 |
| c_nng | 9.4 | 10.8 | 13.1 | 15.5 | 18.4 |
| nng_sync1 | 12.2 | 14.4 | 22.7 | 57.2 | 61.4 |
| nng_sync2 | 35.0 | 49.2 | 64.4 | 70.7 | 81.7 |
| nng_async1 | 35.3 | 36.7 | 48.8 | 50.8 | 51.2 |
| nng_async2 | 40.3 | 51.6 | 61.9 | 86.2 | 86.5 |
| nng_async3 | 224.1 | 236.1 | 242.6 | 243.4 | 243.6 |
| nng_async4 | 34.9 | 44.8 | 54.4 | 60.1 | 69.2 |
| nng_async5 | 23.0 | 33.5 | 43.3 | 47.1 | 66.0 |
| nng_async6 | 323.9 | 332.3 | 342.9 | 343.6 | 343.8 |
| nng_async7 | 41.1 | 108.9 | 206.8 | 221.6 | 225.3 |
| zmq_sync | 8.1 | 9.5 | 13.2 | 19.4 | 20.4 |
| zmq_async | 87.4 | 90.7 | 104.3 | 113.9 | 137.9 |
| pynng_sync | 15.8 | 16.4 | 20.9 | 25.5 | 26.7 |
| pynng_async | 106.1 | 134.8 | 211.2 | 260.2 | 286.9 |

### Latency — ipc — 64 B

| Method | min µs | p50 µs | p95 µs | p99 µs | max µs |
|--------|-------:|-------:|-------:|-------:|-------:|
| python_raw | 21.5 | 22.1 | 23.8 | 24.8 | 26.2 |
| c_nng | 21.1 | 23.2 | 26.8 | 28.6 | 30.1 |
| nng_sync1 | 26.1 | 30.5 | 36.5 | 39.2 | 40.7 |
| nng_sync2 | 55.3 | 73.7 | 83.6 | 86.0 | 88.4 |
| nng_async1 | 47.3 | 54.6 | 65.6 | 71.3 | 74.4 |
| nng_async2 | 55.2 | 69.6 | 81.8 | 83.9 | 86.0 |
| nng_async3 | 279.5 | 285.2 | 295.6 | 295.9 | 296.0 |
| nng_async4 | 56.7 | 67.8 | 81.3 | 89.1 | 118.8 |
| nng_async5 | 40.2 | 53.3 | 63.1 | 65.2 | 81.2 |
| nng_async6 | 302.7 | 311.1 | 321.2 | 321.6 | 321.7 |
| nng_async7 | 138.1 | 200.1 | 222.6 | 223.2 | 223.3 |
| zmq_sync | 24.6 | 25.2 | 27.5 | 31.5 | 32.8 |
| zmq_async | 91.4 | 93.5 | 100.2 | 109.9 | 128.5 |
| pynng_sync | 33.4 | 36.4 | 40.7 | 48.0 | 49.3 |
| pynng_async | 123.8 | 160.1 | 225.0 | 239.4 | 296.3 |

### Latency — tcp — 64 B

| Method | min µs | p50 µs | p95 µs | p99 µs | max µs |
|--------|-------:|-------:|-------:|-------:|-------:|
| python_raw | 28.5 | 29.2 | 32.1 | 35.1 | 35.4 |
| c_nng | 27.6 | 30.4 | 39.7 | 57.0 | 74.6 |
| nng_sync1 | 31.0 | 33.2 | 40.2 | 41.3 | 41.6 |
| nng_sync2 | 69.0 | 77.7 | 85.7 | 91.6 | 101.7 |
| nng_async1 | 53.5 | 60.8 | 71.3 | 75.7 | 76.9 |
| nng_async2 | 67.1 | 78.7 | 90.6 | 91.8 | 99.0 |
| nng_async3 | 334.1 | 335.1 | 339.0 | 339.8 | 340.1 |
| nng_async4 | 63.9 | 74.6 | 86.4 | 89.7 | 90.1 |
| nng_async5 | 46.9 | 61.5 | 71.7 | 73.5 | 79.3 |
| nng_async6 | 292.1 | 299.7 | 307.6 | 308.2 | 308.4 |
| nng_async7 | 242.3 | 263.7 | 294.8 | 297.7 | 298.5 |
| zmq_sync | 30.2 | 30.7 | 33.8 | 38.7 | 41.1 |
| zmq_async | 98.6 | 101.3 | 107.0 | 122.1 | 159.7 |
| pynng_sync | 38.1 | 39.6 | 49.6 | 51.0 | 58.1 |
| pynng_async | 146.8 | 230.7 | 353.6 | 392.6 | 404.3 |


### Ops/sec — inproc — 64 B

| Method | ops/sec |
|--------|--------:|
| python_raw | 128,327 |
| c_nng | 93,319 |
| nng_sync1 | 79,534 |
| nng_sync2 | 21,264 |
| nng_async1 | 29,330 |
| nng_async2 | 19,641 |
| nng_async3 | 44,761 |
| nng_async4 | 22,230 |
| nng_async5 | 30,192 |
| nng_async6 | 33,048 |
| nng_async7 | 58,125 |
| zmq_sync | 107,673 |
| zmq_async | 11,045 |
| pynng_sync | 59,580 |
| pynng_async | 7,211 |


### Ops/sec — ipc — 64 B

| Method | ops/sec |
|--------|--------:|
| python_raw | 46,297 |
| c_nng | 44,415 |
| nng_sync1 | 34,724 |
| nng_sync2 | 13,866 |
| nng_async1 | 18,232 |
| nng_async2 | 14,267 |
| nng_async3 | 33,888 |
| nng_async4 | 14,574 |
| nng_async5 | 19,580 |
| nng_async6 | 31,778 |
| nng_async7 | 45,155 |
| zmq_sync | 37,507 |
| zmq_async | 10,607 |
| pynng_sync | 26,925 |
| pynng_async | 5,818 |


### Ops/sec — tcp — 64 B

| Method | ops/sec |
|--------|--------:|
| python_raw | 34,114 |
| c_nng | 31,796 |
| nng_sync1 | 28,362 |
| nng_sync2 | 12,641 |
| nng_async1 | 15,955 |
| nng_async2 | 12,837 |
| nng_async3 | 29,761 |
| nng_async4 | 13,406 |
| nng_async5 | 16,992 |
| nng_async6 | 27,094 |
| nng_async7 | 37,728 |
| zmq_sync | 30,222 |
| zmq_async | 9,993 |
| pynng_sync | 24,407 |
| pynng_async | 4,797 |

## Interpretation

The benchmarks do a roundtrip of a single message of 64 bytes from a client to a server. The server sends back an empty acknowledgement message. The latency is measured on the client side, from the moment the message is sent to the moment the acknowledgement is received.
The ops/sec is measured as the number of roundtrips that can be performed in one second.

Let's start by the obvious: Without surprise, inproc (inter-thread) transport is the fastest, followed by ipc (inter-process) and tcp (network). This is unsurprising as inproc avoid any copy of the message, while ipc uses faster os mechanisms for inter-process communication than tcp.

The second obvious fact from the benchmarks is that using a raw communication protocol without any abstraction (python_raw, which uses queues for instance for the inproc transport) is the fastest way to transmit data. This is because it avoids any overhead of the messaging library, such as message framing, protocol handling, etc. Naturally, for real applications (not benchmarks), that means that more failure cases should be handled by the application code, which may be error-prone, less maintainable, and may add overhead.

Now for the less obvious: The second fastest options are single threads spawning blocking send and receive calls. This corresponds to c_nng, nng_sync1, zmq_sync and pynng_sync. When doing blocking calls, the implementations avoid thread switching, and the internal logic is simpler. This results in lower latency and higher throughput. We can notice that c_nng, which is a direct C equivalent of nng_sync1, is only slightly faster than the Python implementation, which highlights the efficiency of the Python bindings.

This doesn't mean, though, that the best performance is achieved with blocking calls. In practice you may want to process other work while waiting for messages. This involves either running the `recv` in a separate thread, or maybe using several threads each sending blocking `send` and `recv` calls. Some benchmarks explore the performance impact of these approaches: nng_sync2 submits the `send` and `recv` calls and gets a future that gets resolved in a separate thread (which is similar to running recv yourself in a separate thread). nng_async4 is almost identical to nng_sync2, and nng_async7 runs blocking calls in several threads. These approaches induce a latency overhead and an impact on the throughput.

A second approach to avoid blocking calls is to use Python asyncio and the asynchronous API of nng. This is what nng_async1, nng_async2 and nng_async3 do. Both nng_async1 and nng_async2 issue consecutive `asend` and `arecv` calls in a single thread. nng_async1 uses a slightly faster paradigm than nng_async2, but a less convenient one (the nng_async2 version is recommended for real applications, and is what is used in the examples). nng_async3 parallelizes the `asend` and `arecv` calls with several socket contexts, but still in a single thread. This increases message latency, but improves throughput. If you intend to use asyncio and potentially process several messages in parallel, this competitor is the one to look at.

Overall, the benchmarks show that these two approaches to avoid blocking computations induce a non-negligible overhead. In a real application where the messages have to be built and processed, that overhead, though, may be negligible compared to the time spent building and processing the messages. In that case, convenience is key, and all approaches are valid in the sense that there is not one that performs significantly worse than the others.

Finally, the benchmarks also compare to two other messaging libraries: the Python wrapping of ZeroMQ (version 27.1.0) and another Python wrapper of NNG (PyNNG version 0.9.0). ZeroMQ achieves slightly better performance, but it has to be put in context: NNG brings thread-safety, and the REP/REQ pattern of NNG brings more guarantees (see ZeroMQ documentation and NanoMsg blog for why in practice ZeroMQ's REP/REQ is not of much use beyond toy examples). ZeroMQ's other patterns (for instance the powerful ROUTER) have slightly higher overhead. As for PyNNG, the sync APIs offer similar performance. Python-nng, though, has further optimized the asyncio integration, which shows in the benchmarks.

In conclusion, if you want a message passing library, python-nng offers good performance. NNG provides a user-friendly API which avoids many pitfalls of other messaging libraries, and the Python bindings are efficient. Happy coding!
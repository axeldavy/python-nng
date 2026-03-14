# nng

High-performance Python bindings for [nng](https://nng.nanomsg.org/) (nanomsg-next-gen), written in Cython.

## Features

- Full coverage of the nng v2 SP (Scalability Protocols) API
- Zero-copy message bodies via the Python buffer protocol
- asyncio integration via nng's native AIO callbacks (no thread-pool overhead)
- Strong typing with Cython `cdef` classes
- All seven SP protocols: Pair, Pub/Sub, Req/Rep, Push/Pull, Surveyor/Respondent, Bus
- TLS support
- Protocol contexts for concurrent request handling

## Build

```bash
uv pip install --no-build-isolation -e .
```

## Quick start

```python
import nng

with nng.RepSocket() as rep, nng.ReqSocket() as req:
    rep.listen("inproc://demo")
    req.dial("inproc://demo")
    req.send(b"hello")
    print(rep.recv())   # b'hello'
```

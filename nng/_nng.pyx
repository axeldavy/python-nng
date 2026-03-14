# cython: language_level=3
# cython: binding=True
# cython: cdivision=True
# cython: boundscheck=False
# cython: wraparound=False
#
# nng/_nng.pyx
# ─────────────────────────────────────────────────────────────────────────────
# Single Cython extension module for all nng Python bindings.
# All protocol classes, error types, and async infrastructure live here.
#
# Build: CMakeLists.txt invokes Cython with -I nng/, which makes
#        _decls.pxd visible.  The _*.pxi files are included below.

from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t, int32_t, int16_t
from libc.stddef cimport size_t
from libc.string cimport memset
from cpython.buffer cimport PyBUF_FORMAT, Py_buffer
from cpython.ref cimport Py_INCREF, Py_DECREF

cimport cython

# Pull in all the corrected C declarations
from nng._decls cimport *

# Pull in the C trampoline header (ensures nng.h + Python.h are both visible)
cdef extern from "_trampoline.h":
    pass

# ─────────────────────────────────────────────────────────────────────────────
# NNG init
# ─────────────────────────────────────────────────────────────────────────────

import atexit as _atexit
import threading as _threading

cdef int _nng_initialized = 0
cdef object _nng_init_lock = _threading.Lock()


cdef void check_nng_init() except *:
    """Ensure nng_init() has been called; auto-initialise with defaults if not.

    Thread-safe singleton: uses a double-checked locking pattern with the
    Python threading.Lock (GIL is always held when this is called from
    Cython __cinit__ / cdef code paths).
    """
    global _nng_initialized
    cdef nng_err rv
    if _nng_initialized:
        return
    with _nng_init_lock:
        if _nng_initialized:
            return
        rv = nng_init(NULL)
        if rv != NNG_OK:
            raise RuntimeError(
                f"nng auto-init failed: {nng_strerror(<nng_err>rv).decode('ascii', 'replace')}"
            )
        _nng_initialized = 1


def _nng_fini():
    """Shut down the nng library (called automatically via atexit)."""
    global _nng_initialized
    if _nng_initialized:
        nng_fini()
        _nng_initialized = 0


_atexit.register(_nng_fini)


# ─────────────────────────────────────────────────────────────────────────────
# Included implementation sections (order matters for forward references)
# ─────────────────────────────────────────────────────────────────────────────

include "_errors.pxi"    # NngError hierarchy, check_err()
include "_message.pxi"   # Message: zero-copy nng_msg* wrapper
include "_tls.pxi"       # TlsConfig, TlsCert
include "_aio.pxi"       # _AioOp, _aio_trampoline, _in_flight_ops
include "_dialer.pxi"    # Dialer, Listener
include "_context.pxi"   # Context (nng_ctx)
include "_socket.pxi"    # Socket base + all protocol subclasses

# ─────────────────────────────────────────────────────────────────────────────
# Module-level utilities
# ─────────────────────────────────────────────────────────────────────────────

def version() -> str:
    """Return the nng library version string (e.g. ``"2.0.0-dev"``)."""
    return nng_version().decode("ascii")

def random() -> int:
    """Return a 32-bit cryptographically random value."""
    check_nng_init()
    return nng_random()


# ─────────────────────────────────────────────────────────────────────────────
# User-facing initialisation
# ─────────────────────────────────────────────────────────────────────────────

def initialize(*,
             int num_task_threads=0,
             int max_task_threads=-1,
             int num_expire_threads=0,
             int max_expire_threads=-1,
             int num_poller_threads=0,
             int max_poller_threads=-1,
             int num_resolver_threads=0) -> None:
    """Initialise the nng library with custom threading parameters.

    Must be called **before** creating any nng object or calling any nng
    function.  If not called explicitly, the library is auto-initialised with
    default parameters on first use.

    Parameters (all keyword-only, default 0 / -1 = nng default / no cap):
        num_task_threads     – fixed number of task-queue threads (0 = auto)
        max_task_threads     – cap on task-queue threads (-1 = no cap)
        num_expire_threads   – fixed number of expiry threads (0 = auto)
        max_expire_threads   – cap on expiry threads (-1 = no cap)
        num_poller_threads   – fixed number of I/O poller threads (0 = auto)
        max_poller_threads   – cap on poller threads (-1 = no cap)
        num_resolver_threads – fixed number of DNS resolver threads (0 = auto)

    Raises RuntimeError if nng is already initialised or if nng_init() fails.
    """
    global _nng_initialized
    cdef nng_init_params p
    cdef nng_err rv
    with _nng_init_lock:
        if _nng_initialized:
            raise RuntimeError(
                "nng is already initialised. "
                "Call nng_init() before creating any nng object or function."
            )
        memset(&p, 0, sizeof(p))
        p.num_task_threads    = <int16_t>num_task_threads
        p.max_task_threads    = <int16_t>max_task_threads
        p.num_expire_threads  = <int16_t>num_expire_threads
        p.max_expire_threads  = <int16_t>max_expire_threads
        p.num_poller_threads  = <int16_t>num_poller_threads
        p.max_poller_threads  = <int16_t>max_poller_threads
        p.num_resolver_threads = <int16_t>num_resolver_threads
        rv = nng_init(&p)
        if rv != NNG_OK:
            raise RuntimeError(
                f"nng_init() failed: {nng_strerror(<nng_err>rv).decode('ascii', 'replace')}"
            )
        _nng_initialized = 1

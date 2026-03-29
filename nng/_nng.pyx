#cython: language_level=3
#cython: binding=True
#cython: cdivision=True
#cython: boundscheck=False
#cython: wraparound=False
#cython: nonecheck=False
#cython: embedsignature=False
#cython: cdivision=True
#cython: cdivision_warnings=False
#cython: always_allow_keywords=False
#cython: profile=False
#cython: infer_types=False
#cython: initializedcheck=False
#cython: c_line_in_traceback=False
#cython: auto_pickle=False
#cython: freethreading_compatible=True
#distutils: language=c++


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
from libc.string cimport memset, memcpy
from libcpp cimport bool as cpp_bool
from libcpp.utility cimport move
from cpython.buffer cimport PyBUF_FORMAT, Py_buffer
from cpython.exc cimport PyErr_CheckSignals
from cpython.ref cimport Py_INCREF, Py_DECREF


from weakref import ref as _weakref, \
    WeakValueDictionary as _WeakValueDictionary, \
    WeakKeyDictionary as _WeakKeyDictionary

cimport cython

# Pull in all the corrected C declarations
from nng._decls cimport *

# Pull in the C++ RAII handle classes and shared_ptr/make_shared
from nng._cpp cimport *

# Mutex from DearCyGui
from nng._mutex cimport unique_lock, DCGMutex, defer_lock_t

cdef void lock_gil_friendly_block(unique_lock[DCGMutex] &m) noexcept:
    """
    Same as lock_gil_friendly, but blocks until the job is done.
    We inline the fast path, but not this one as it generates
    more code.
    """
    # Release the gil to enable python processes eventually
    # holding the lock to run and release it.
    # Block until we get the lock
    cdef bint locked = False
    while not(locked):
        with nogil:
            # Block until the mutex is released
            m.lock()
            # Unlock to prevent deadlock if another
            # thread holding the gil requires m
            # somehow
            m.unlock()
        locked = m.try_lock()

cdef inline void lock_gil_friendly(unique_lock[DCGMutex] &m,
                                   DCGMutex &mutex) noexcept:
    """
    Must be called to lock our mutexes whenever we hold the gil
    """
    m = unique_lock[DCGMutex](mutex, defer_lock_t())
    # Fast path which will be hit almost always
    if m.try_lock():
        return
    # Slow path
    lock_gil_friendly_block(m)

# Pull in the C trampoline header (ensures nng.h + Python.h are both visible)
cdef extern from "_trampoline.h":
    pass

# ─────────────────────────────────────────────────────────────────────────────
# NNG init
# ─────────────────────────────────────────────────────────────────────────────

import atexit as _atexit
import gc as _gc
import threading as _threading
from traceback import print_exc as _print_exc

cdef int _nng_initialized = 0
cdef object _nng_init_lock = _threading.Lock()

# ── Pipe event refresh in dispatch thread  ──────────────────────────────────────────────────────────

# Weak registry: int(socket_id) → Socket.  Populated at socket creation
_SOCKET_REGISTRY = _WeakValueDictionary()

# Same with pipes
_PIPE_REGISTRY = _WeakValueDictionary()


cdef void _drain_pipe_events():
    """
    Drain all pending pipe events.

    Called only from the dispatcher thread
    """

    for socket in _SOCKET_REGISTRY.values():
        (<Socket>socket).update_pipes()


# ── AIO dispatch queue ────────────────────────────────────────────────────────
# Allocated at module import; always valid.  The nng callback thread calls
# push() (GIL-free).  The dispatcher thread drains via get_ready/wait_for.
cdef DispatchQueue* _DISPATCH_QUEUE = new DispatchQueue()


# Maps id(op) -> op._dispatch_complete bound method.
# Written under the GIL before submit; read and erased under the GIL in the
# dispatcher thread.
cdef object _DISPATCH_CALLABLES = {}

# Dispatcher thread handle (None until first nng_init).
cdef object _DISPATCH_THREAD = None


cdef inline void _dispatch_one(uint64_t op_id):
    """Pop and invoke the callable registered for op_id (GIL held)."""
    if op_id == 0:
        # Sentinel value: just drain pipe events.
        _drain_pipe_events()
        return

    # Look up the op_id in the registry and pop the callback.
    cb = _DISPATCH_CALLABLES.pop(op_id, None)

    # Call the callback if found; ignore if not (e.g. if the op was cancelled and its callback removed).
    if cb is not None:
        try:
            cb()
        except BaseException:
            _print_exc()



cdef void _start_DISPATCH_THREAD():
    """Start the dispatcher daemon thread (called from check_nng_init / initialize)."""
    global _DISPATCH_THREAD
    if _DISPATCH_THREAD is not None and _DISPATCH_THREAD.is_alive():
        return
    _DISPATCH_THREAD = _threading.Thread(
        target=_aio_dispatch_loop,
        name="nng-aio-dispatcher",
        daemon=True,
    )
    _DISPATCH_THREAD.start()


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
        _start_DISPATCH_THREAD()


def _nng_fini():
    """Shut down the nng library (called automatically via atexit)."""
    global _nng_initialized, _DISPATCH_THREAD
    if _nng_initialized:
        # Stop the dispatch queue so the dispatcher thread exits its loop.
        _DISPATCH_QUEUE.stop()
        if _DISPATCH_THREAD is not None:
            _DISPATCH_THREAD.join(timeout=2.0)
            _DISPATCH_THREAD = None
        # Clear in-flight op references before calling nng_fini().
        # _DISPATCH_CALLABLES holds bound methods that keep _AioCbSync /
        # _AioCbASync objects alive.  Dropping those refs now triggers
        # AioHandle::~AioHandle() → nng_aio_free() while nng is still up.
        # gc.collect() breaks any surviving reference cycles for the same
        # reason (e.g. op._future ↔ future._done_callbacks[op._on_cancel]).
        _DISPATCH_CALLABLES.clear()
        _gc.collect()
        nng_fini()
        _nng_initialized = 0


_atexit.register(_nng_fini)

# ─────────────────────────────────────────────────────────────────────────────
# Included implementation sections (order matters for forward references)
# ─────────────────────────────────────────────────────────────────────────────

include "_errors.pxi"    # NngError hierarchy, check_err()
include "_message.pxi"   # Message: zero-copy nng_msg* wrapper
include "_tls.pxi"       # TlsCert, TlsConfig, tls_engine_name, tls_engine_description
include "_aio_async.pxi"       # _AioCbASync, _aio_cb_async_trampoline
include "_aio_cb_sync.pxi"        # _AioCbSync, _aio_cb_sync_trampoline
include "_context.pxi"   # Context (nng_ctx)
include "_dialer.pxi"    # Dialer
include "_listener.pxi"  # Listener

include "_socket.pxi"    # Socket base + all protocol subclasses

# ─────────────────────────────────────────────────────────────────────────────
# event dispatch
# ─────────────────────────────────────────────────────────────────────────────




def _aio_dispatch_loop():
    """Target for the dispatcher thread.

    Drains _DISPATCH_QUEUE and _pipe_event_queue without holding the GIL
    during the wait, so nng's callback threads are never blocked on Python.
    """
    cdef uint64_t op_id = 0
    cdef bint got = False
    cdef uint32_t psid = 0
    cdef uint32_t ppid = 0
    cdef int pev = 0
    cdef bint pgot = False

    while True:
        # 1. Non-blocking drain: pick up all AIO completions.
        got = _DISPATCH_QUEUE.get_ready(op_id)
        while got:
            _dispatch_one(op_id)
            got = _DISPATCH_QUEUE.get_ready(op_id)

        # 2. Check stop conditions before blocking.
        if _DISPATCH_QUEUE.is_stopped():
            break

        # 3. Block (GIL released) waiting for the next item or a 150 ms wake-up.
        #    The short timeout lets us re-check interpreter state periodically.
        with nogil:
            got = _DISPATCH_QUEUE.wait_for(op_id, 150)

        if got:
            _dispatch_one(op_id)


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
        _start_DISPATCH_THREAD()

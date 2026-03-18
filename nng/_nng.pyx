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

from cpython.ref cimport Py_INCREF, Py_DECREF

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
import threading as _threading
from traceback import print_exc as _print_exc

cdef int _nng_initialized = 0
cdef object _nng_init_lock = _threading.Lock()

# ── AIO dispatch queue ────────────────────────────────────────────────────────
# Allocated at module import; always valid.  The nng callback thread calls
# push() (GIL-free).  The dispatcher thread drains via get_ready/wait_for.
cdef DispatchQueue* _dispatch_queue = new DispatchQueue()

# ── Pipe event queue ──────────────────────────────────────────────────────────
# GIL-free queue for pipe lifecycle events.  The nng callback thread pushes
# (pipe_id, ev) pairs; the dispatcher thread drains them after being woken
# by a sentinel 0 push into _dispatch_queue.
cdef PipeEventQueue* _pipe_event_queue = new PipeEventQueue()

# Weak registry: int(socket_id) → Socket.  Populated at socket creation;
# entries vanish automatically when the Socket is garbage-collected.
import weakref as _weakref
_socket_registry = _weakref.WeakValueDictionary()

# Maps id(op) -> op._dispatch_complete bound method.
# Written under the GIL before submit; read and erased under the GIL in the
# dispatcher thread.
cdef object _dispatch_callables = {}

# Dispatcher thread handle (None until first nng_init).
cdef object _dispatch_thread = None


cdef inline void _dispatch_one(uint64_t op_id):
    """Pop and invoke the callable registered for op_id (GIL held)."""
    cb = _dispatch_callables.pop(op_id, None)
    if cb is not None:
        try:
            cb()
        except BaseException:
            _print_exc()



cdef void _start_dispatch_thread():
    """Start the dispatcher daemon thread (called from check_nng_init / initialize)."""
    global _dispatch_thread
    if _dispatch_thread is not None and _dispatch_thread.is_alive():
        return
    _dispatch_thread = _threading.Thread(
        target=_aio_dispatch_loop,
        name="nng-aio-dispatcher",
        daemon=True,
    )
    _dispatch_thread.start()


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
        _start_dispatch_thread()


def _nng_fini():
    """Shut down the nng library (called automatically via atexit)."""
    global _nng_initialized, _dispatch_thread
    if _nng_initialized:
        # Stop both queues so the dispatcher thread exits its loop.
        _dispatch_queue.stop()
        _pipe_event_queue.stop()
        if _dispatch_thread is not None:
            _dispatch_thread.join(timeout=2.0)
            _dispatch_thread = None
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
include "_context.pxi"   # Context (nng_ctx)
include "_dialer.pxi"    # Dialer
include "_listener.pxi"  # Listener

include "_socket.pxi"    # Socket base + all protocol subclasses

# ─────────────────────────────────────────────────────────────────────────────
# Pipe event dispatch  (defined here, after _socket.pxi, so Socket / Pipe /
# PipeStatus are available; called from _aio_dispatch_loop below)
# ─────────────────────────────────────────────────────────────────────────────

cdef inline void _dispatch_pipe_event(uint32_t sock_id, uint32_t pipe_id, int ev):
    """Process one pipe lifecycle event from _pipe_event_queue (GIL held).

    sock_id was captured in the trampoline while the pipe was still alive;
    do NOT call nng_pipe_socket() here — for REM_POST the pipe handle is
    already destroyed and the call would return an invalid id.
    """
    cdef nng_pipe p
    p.id = pipe_id
    sock = _socket_registry.get(sock_id)
    if sock is None:
        return
    cdef Socket s = <Socket>sock
    if ev == NNG_PIPE_EV_ADD_PRE:
        s.add_pipe(p)
    elif ev == NNG_PIPE_EV_ADD_POST:
        s.promote_pipe(pipe_id)
    elif ev == NNG_PIPE_EV_REM_POST:
        s.remove_pipe(pipe_id)


def _aio_dispatch_loop():
    """Target for the dispatcher thread.

    Drains _dispatch_queue and _pipe_event_queue without holding the GIL
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
        got = _dispatch_queue.get_ready(op_id)
        while got:
            _dispatch_one(op_id)
            got = _dispatch_queue.get_ready(op_id)

        # 2. Drain all pending pipe events.
        pgot = _pipe_event_queue.get_ready(psid, ppid, pev)
        while pgot:
            _dispatch_pipe_event(psid, ppid, pev)
            pgot = _pipe_event_queue.get_ready(psid, ppid, pev)

        # 3. Check stop conditions before blocking.
        if _dispatch_queue.is_stopped():
            break

        # 4. Block (GIL released) waiting for the next item or a 150 ms wake-up.
        #    The short timeout lets us re-check interpreter state periodically.
        with nogil:
            got = _dispatch_queue.wait_for(op_id, 150)

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
        _start_dispatch_thread()

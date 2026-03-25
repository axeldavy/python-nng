# nng/_aio_sock_async.pxi – included into _nng.pyx
#
# _AioSockASync – internal class managing one in-flight async nng operation,
# with a callback trampoline and asyncio Future resolution.
#
# Callback flow (two-part trampoline):
#
#   Part 1 – _aio_sock_async_trampoline()   (nng thread, NO GIL)
#     • casts arg to DispatchQueueContainer* and calls fire()
#     • fire() enqueues op_id in the fd-based IDispatchQueue and writes a
#       wakeup byte to the notification socket/pipe
#
#   Part 2 – _AioSockASync._dispatch_complete()   (asyncio event-loop thread, GIL held)
#     • nng_aio_result() to get the error code
#     • resolve asyncio Future via loop.call_soon_threadsafe()

from asyncio import CancelledError as _CancelledError, \
    get_running_loop as _get_running_loop

cimport cython

# ── Part 1: GIL-free trampoline ───────────────────────────────────────────────

cdef void _aio_sock_async_trampoline(void *arg) noexcept nogil:
    """Called by nng's thread pool on op completion – no GIL, no Python."""
    # fire() enqueues op_id and writes a wakeup byte to the notification fd.
    (<DispatchQueueContainer*>arg).fire()

# ── asyncio helpers  ──

def _set_future_done(fut):
    """Resolve an asyncio Future successfully (must run on the loop thread)."""
    if not fut.done():
        fut.set_result(None)

def _set_future_exception(fut, exc):
    """Resolve an asyncio Future with an error (must run on the loop thread)."""
    if not fut.done():
        fut.set_exception(exc)

# ── _AioSockASync ────────────────────────────────────────────────────────────────────


cdef class _AioSockASync:
    """One in-flight async nng operation."""

    cdef unique_ptr[AioHandle] _handle
    # _container holds a shared_ptr to the queue and the op-id to push.
    # Its raw pointer is registered as the nng_aio callback arg, so it must
    # outlive the nng_aio.  Lifetime is managed: _handle is reset first in
    # __dealloc__ (nng_aio_free ensures the trampoline will never fire again),
    # then _container is freed automatically.
    cdef unique_ptr[DispatchQueueContainer] _container
    cdef object _dispatch_callable
    cdef object _loop      # asyncio event loop
    cdef object _future    # asyncio.Future

    def __cinit__(self):
        # C++ members are default-constructed (empty) by Cython's member glue.
        self._loop        = None
        self._future      = None

    def __init__(self):
        raise RuntimeError("_AioSockASync cannot be instantiated directly")

    def __dealloc__(self):
        self._future  = None
        self._loop    = None
        # Reset the AIO handle first: nng_aio_free guarantees the trampoline
        # will never fire after this returns, so the container pointer stored
        # as nng's void* arg is safe to release immediately afterwards.
        self._handle.reset()

    # ── Factory methods ───────────────────────────────────────────────────

    @staticmethod
    cdef _AioSockASync create_for_socket(
        shared_ptr[SocketHandle] anchor,
        shared_ptr[IDispatchQueue] dispatch_queue,
        object dispatch_callable
    ):
        """Allocate a new _AioSockASync anchored to *anchor* (a SocketHandle)."""
        check_nng_init()
        cdef _AioSockASync op = _AioSockASync.__new__(_AioSockASync)

        # Retrieve loop
        op._loop   = _get_running_loop()
        if op._loop is None:
            raise RuntimeError("No running event loop found")

        # Create the future that will be resolved on completion
        op._future = op._loop.create_future()

        # Create the container: stores queue ref + op address (= id(op)).
        # Must be done before nng_aio_alloc so the raw pointer is stable.
        op._container = make_unique[DispatchQueueContainer](
            dispatch_queue, <uint64_t><void*>op
        )

        # Initialize the aio with the container as callback arg.
        cdef nng_aio *raw_aio = NULL
        check_err(nng_aio_alloc(&raw_aio, _aio_sock_async_trampoline,
                                op._container.get()))
        op._handle = make_unique[AioHandle](raw_aio, anchor)
        op._dispatch_callable = dispatch_callable
        return op

    @staticmethod
    cdef _AioSockASync create_for_context(
        shared_ptr[ContextHandle] anchor,
        shared_ptr[IDispatchQueue] dispatch_queue,
        object dispatch_callable
    ):
        """Allocate a new _AioSockASync anchored to *anchor* (a ContextHandle)."""
        check_nng_init()
        cdef _AioSockASync op = _AioSockASync.__new__(_AioSockASync)

        # Retrieve loop
        op._loop   = _get_running_loop()
        if op._loop is None:
            raise RuntimeError("No running event loop found")

        # Create the future that will be resolved on completion
        op._future = op._loop.create_future()

        # Create the container: stores queue ref + op address (= id(op)).
        # Must be done before nng_aio_alloc so the raw pointer is stable.
        op._container = make_unique[DispatchQueueContainer](
            dispatch_queue, <uint64_t><void*>op
        )

        # Initialize the aio with the container as callback arg.
        cdef nng_aio *raw_aio = NULL
        check_err(nng_aio_alloc(&raw_aio, _aio_sock_async_trampoline,
                                op._container.get()))
        op._handle = make_unique[AioHandle](raw_aio, anchor)
        op._dispatch_callable = dispatch_callable
        return op

    # ── Part 2: dispatcher-thread completion handler ──────────────────────

    def _dispatch_complete(self):
        """Resolve the future; called inside the loop (GIL held).

        nng_aio_result() is safe to call here: by the time the dispatcher runs
        this, the callback has already returned and the result is stable.
        """
        cdef int err

        fut  = self._future
        loop = self._loop
        self._future  = None
        self._loop    = None

        if fut is None or loop is None:
            return

        try:
            running_loop = _get_running_loop()
        except RuntimeError:
            running_loop = None

        if loop != running_loop:
            # Can occur only during _AioAsyncManager release(),
            # when the manager cancels all in-flight ops and waits for them to finish.
            self._future = fut
            self._loop = loop
            return self._dispatch_complete_safe()

        # The trampoline might not have returned in some rare race cases,
        # but also can occur during release() in which case the trampoline
        # might not have been fired yet. In either case, we can still safely wait.
        if nng_aio_busy(self._handle.get().get()):
            nng_aio_wait(self._handle.get().get())

        # Retrieve the result code once the aio is finished
        err = nng_aio_result(self._handle.get().get())

        # We are already on the event-loop thread (asserted above), so resolve
        # the future directly.
        if err == NNG_OK:
            if not fut.done():
                fut.set_result(None)
        else:
            if not fut.done():
                fut.set_exception(_err_from_code(err))

    def _dispatch_complete_safe(self):
        """Resolve the future; called by the dispatcher thread (GIL held) (or _AioAsyncManager release()).

        nng_aio_result() is safe to call here: by the time the dispatcher runs
        this, the callback has already returned and the result is stable.
        """
        cdef int err

        fut  = self._future
        loop = self._loop
        self._future  = None
        self._loop    = None

        if fut is None or loop is None:
            return

        # The trampoline might not have returned in some rare race cases,
        # but also can occur during release() in which case the trampoline
        # might not have been fired yet. In either case, we can still safely wait.
        if nng_aio_busy(self._handle.get().get()):
            nng_aio_wait(self._handle.get().get())

        # Retrieve the result code once the aio is finished
        err = nng_aio_result(self._handle.get().get())

        # asyncio mode: schedule on the event-loop thread.
        # the asyncio recv methods retrieve the message themselves after await.
        if err == NNG_OK:
            loop.call_soon_threadsafe(_set_future_done, fut)
        else:
            loop.call_soon_threadsafe(
                _set_future_exception, fut, _err_from_code(err))

    # ── Pre-submission helpers ────────────────────────────────────────────

    @cython.final
    cdef inline void set_timeout(self, int32_t ms) noexcept nogil:
        """Set the timeout for this operation (in milliseconds)."""
        nng_aio_set_timeout(self._handle.get().get(), ms)

    @cython.final
    cdef void set_msg(self, nng_msg *ptr) noexcept nogil:
        """Attach an nng_msg to the underlying aio (for send operations)."""
        nng_aio_set_msg(self._handle.get().get(), ptr)

    # ── Post-completion helpers ───────────────────────────────────────────

    @cython.final
    cdef nng_msg *get_msg(self) noexcept:
        """Retrieve the received nng_msg from the aio (for recv operations).

        Returns NULL if no message is present.
        """
        return nng_aio_get_msg(self._handle.get().get())

    # ── Submission ────────────────────────────────────────────────────────

    cdef int _prepare_submit(self):
        """Register the completion callable and keep op alive until dispatch.

        Must be called (with the GIL held) immediately before the nng call.
        """
        # Register the dispatch callable keyed by this object's address.
        self._dispatch_callable[id(self)] = self._dispatch_complete
        return 0

    cdef int _cancel_dispatch(self) noexcept:
        """Unregister the dispatch callable to prevent future dispatch."""
        if self._dispatch_callable is not None:
            self._dispatch_callable.pop(id(self), None)
        return 0

    @cython.final
    cdef bint submit_socket_send(self, nng_socket sock):
        """
        Submit this op as a socket send.
        
        Return True if the operation is already completed
        """
        self._prepare_submit()
        with nogil:
            nng_socket_send(sock, self._handle.get().get())

        # The else False is for _AioCbASync which uses a different trampoline
        cdef bint skipped = self._container.get().reach_skip_point() if self._container else False
        if skipped:
            self._cancel_dispatch()
        return skipped

    @cython.final
    cdef bint submit_socket_recv(self, nng_socket sock):
        """
        Submit this op as a socket receive.
        
        Return True if the operation is already completed
        """
        self._prepare_submit()
        with nogil:
            nng_socket_recv(sock, self._handle.get().get())

        cdef bint skipped = self._container.get().reach_skip_point() if self._container else False
        if skipped:
            self._cancel_dispatch()
        return skipped

    @cython.final
    cdef bint submit_ctx_send(self, nng_ctx ctx):
        """
        Submit this op as a context send.
        
        Return True if the operation is already completed
        """
        self._prepare_submit()
        with nogil:
            nng_ctx_send(ctx, self._handle.get().get())

        cdef bint skipped = self._container.get().reach_skip_point() if self._container else False
        if skipped:
            self._cancel_dispatch()
        return skipped

    @cython.final
    cdef bint submit_ctx_recv(self, nng_ctx ctx):
        """Submit this op as a context receive.
        
        Return True if the operation is already completed
        """
        self._prepare_submit()
        with nogil:
            nng_ctx_recv(ctx, self._handle.get().get())

        cdef bint skipped = self._container.get().reach_skip_point() if self._container else False
        if skipped:
            self._cancel_dispatch()
        return skipped

    @cython.final
    cdef object get_future(self):
        """Return the Future associated with this operation.

        Retrieve before submission; after submission the field may be cleared
        by the dispatcher at any time.
        """
        return self._future

    @cython.final
    cdef int on_future_cancel(self) noexcept:
        """Cancel the in-flight operation when the caller's Future is cancelled."""
        with nogil:
            nng_aio_cancel(self._handle.get().get())
            # Wait so that on_future_cancel() returns only after the trampoline
            # has fired (and thus the id is already in the dispatch queue).
            nng_aio_wait(self._handle.get().get())
        return 0

    @cython.final
    cdef int ensure_finish(self) noexcept:
        """Block until the trampoline has fired (and the id queued)."""
        if not nng_aio_busy(self._handle.get().get()):
            return 0
        with nogil:
            nng_aio_stop(self._handle.get().get())
            nng_aio_wait(self._handle.get().get())
        return 0


# ── _AioCbASync ────────────────────────────────────────────────────────────────────
# Variant that doesn't use sockets, but the dispatcher thread

cdef void _aio_cb_async_trampoline(void *arg) noexcept nogil:
    """Called by nng's thread pool on op completion – no GIL, no Python."""
    # Push only the object address as an integer; no Python state touched.
    _DISPATCH_QUEUE.push(<uint64_t><void*>arg)

cdef class _AioCbASync(_AioSockASync):
    """One in-flight async nng operation."""

    # ── Factory methods ───────────────────────────────────────────────────

    @staticmethod
    cdef _AioCbASync create_for_socket_cb(shared_ptr[SocketHandle] anchor):
        """Allocate a new _AioCbASync anchored to *anchor* (a SocketHandle)."""
        check_nng_init()
        cdef _AioCbASync op = _AioCbASync.__new__(_AioCbASync)

        # Retrieve loop
        op._loop   = _get_running_loop()
        if op._loop is None:
            raise RuntimeError("No running event loop found")

        # Create the future that will be resolved on completion
        op._future = op._loop.create_future()

        # Initialize the aio
        cdef nng_aio *raw_aio = NULL
        check_err(nng_aio_alloc(&raw_aio, _aio_cb_async_trampoline, <void *>op))
        op._handle = make_unique[AioHandle](raw_aio, anchor)
        return op

    @staticmethod
    cdef _AioCbASync create_for_context_cb(shared_ptr[ContextHandle] anchor):
        """Allocate a new _AioCbASync anchored to *anchor* (a ContextHandle)."""
        check_nng_init()
        cdef _AioCbASync op = _AioCbASync.__new__(_AioCbASync)

        # Retrieve loop
        op._loop   = _get_running_loop()
        if op._loop is None:
            raise RuntimeError("No running event loop found")

        # Create the future that will be resolved on completion
        op._future = op._loop.create_future()

        # Initialize the aio
        cdef nng_aio *raw_aio = NULL
        check_err(nng_aio_alloc(&raw_aio, _aio_cb_async_trampoline, <void *>op))
        op._handle = make_unique[AioHandle](raw_aio, anchor)
        return op

    # ── Part 2: dispatcher-thread completion handler ──────────────────────


    cdef int _prepare_submit(self):
        """Register the completion callable and keep op alive until dispatch.

        Must be called (with the GIL held) immediately before the nng call.
        """
        global _DISPATCH_CALLABLES

        # Register the dispatch callable keyed by this object's address.
        _DISPATCH_CALLABLES[id(self)] = self._dispatch_complete_safe
        return 0

    cdef int _cancel_dispatch(self) noexcept:
        """Unregister the dispatch callable to prevent future dispatch."""
        global _DISPATCH_CALLABLES
        _DISPATCH_CALLABLES.pop(id(self), None)
        return 0


# ── _AioAsyncManager ──────────────────────────────────────────────────────────
# Import asyncio.ProactorEventLoop if available (Windows only); set to None on
# other platforms so isinstance() checks degrade gracefully.
try:
    from asyncio import ProactorEventLoop as _ProactorEventLoop
except ImportError:
    _ProactorEventLoop = None

# Mode integers stored in _AioAsyncManager._mode.
cdef int _AIO_MGR_POLL     = 0  # PollDispatchQueue + add_reader
cdef int _AIO_MGR_PROACTOR = 1  # SockDispatchQueue + permanent drain task
cdef int _AIO_MGR_FALLBACK = 2  # _AioCbASync via global dispatcher thread


cdef class _AioAsyncManager:
    """Per-loop async dispatch manager for a :class:`Socket`.

    Stored in ``Socket._aio_managers`` (a :class:`~weakref.WeakKeyDictionary`
    mapping *loop* → *manager*).  Created lazily on the first
    :meth:`~Socket.asend` or :meth:`~Socket.arecv` call for a given event
    loop and released when the owning socket is closed.

    Selects the most efficient available dispatch path:

    * **POLL** – :class:`PollDispatchQueue` + ``loop.add_reader`` (default on
      POSIX :class:`~asyncio.SelectorEventLoop` and compatible loops such as
      *uvloop*).  AIO completions are dispatched directly in the I/O callback,
      with no cross-thread hop.
    * **PROACTOR** – :class:`SockDispatchQueue` + a permanent drain task via
      ``loop.sock_recv`` (Windows :class:`~asyncio.ProactorEventLoop`).
      If the drain task is cancelled by user code (not by :meth:`release`),
      subsequent operations fall back to FALLBACK mode automatically.
    * **FALLBACK** – :class:`_AioCbASync` backed by the global dispatcher
      thread.  Always works; slightly higher latency due to the extra thread
      wakeup and ``call_soon_threadsafe`` round-trip.

    Not thread-safe: all public methods must be called with the GIL held.
    """

    cdef object _dispatch_callables          # dict  id(op) → callable
    cdef object _drain_task                  # asyncio.Task (PROACTOR mode)
    cdef object _drain_sock                  # socket.socket wrapper (PROACTOR mode)
    cdef DCGMutex _lock                      # protects all fields
    cdef object _loop                        # asyncio event loop (strong ref)
    cdef int    _mode                        # _AIO_MGR_POLL / _PROACTOR / _FALLBACK
    cdef shared_ptr[IDispatchQueue] _queue   # POLL / PROACTOR modes
    cdef bint   _releasing                   # True when release() initiated the cancel
    cdef bint   _task_user_cancelled         # True after user-cancelled drain task
    cdef int    _read_fd                     # POLL: fd registered with add_reader

    def __cinit__(self):
        """Initialise all fields to safe defaults."""
        self._loop                = None
        self._mode                = _AIO_MGR_FALLBACK
        self._dispatch_callables  = None
        self._drain_task          = None
        self._drain_sock          = None
        self._releasing           = False
        self._task_user_cancelled = False
        self._read_fd             = -1

    def __init__(self):
        """Block direct instantiation; use :meth:`create` instead."""
        raise RuntimeError("Use _AioAsyncManager.create() instead.")

    def __dealloc__(self):
        """Close the dup'd drain socket if one was created."""
        if self._drain_sock is not None:
            try:
                self._drain_sock.close()
            except Exception:
                pass

    # ── Factory ────────────────────────────────────────────────────────────

    @staticmethod
    cdef _AioAsyncManager create(object loop):
        """Allocate a manager for *loop*, choosing the best dispatch mode.

        Args:
            loop: The running :class:`asyncio.AbstractEventLoop`.

        Returns:
            A fully initialised :class:`_AioAsyncManager`.
        """
        cdef _AioAsyncManager mgr = _AioAsyncManager.__new__(_AioAsyncManager)
        mgr._loop = loop
        mgr._select_mode(loop)
        return mgr

    # ── Mode selection ─────────────────────────────────────────────────────

    cdef int _select_mode(self, object loop):
        """Choose and initialise the best available dispatch mode for *loop*."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        # Windows ProactorEventLoop: socket-pair wakeup + drain task.
        if _ProactorEventLoop is not None and isinstance(loop, _ProactorEventLoop):
            if self._try_init_proactor(loop):
                return 0
        else:
            # All other loops (POSIX SelectorEventLoop, uvloop, …): pipe fd.
            if self._try_init_poll(loop):
                return 0

        # Nothing worked; fall back to the global background-thread dispatcher.
        self._mode = _AIO_MGR_FALLBACK
        return 0

    cdef bint _try_init_poll(self, object loop):
        """Attempt POLL mode initialisation; return True on success."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        cdef int fd
        try:
            self._queue = make_poll_dispatch_queue()
            fd = self._queue.get().get_read_fd()
            if fd < 0:
                self._queue.reset()
                return False

            self._dispatch_callables = {}
            self._read_fd            = fd
            self._mode               = _AIO_MGR_POLL
            loop.add_reader(fd, self._poll_readable_callback)
            return True
        except Exception:
            self._queue.reset()
            self._dispatch_callables = None
            return False

    cdef bint _try_init_proactor(self, object loop):
        """Attempt PROACTOR mode initialisation; return True on success."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        import socket as _socket_module
        cdef int fd
        try:
            self._queue = make_sock_dispatch_queue()
            fd = self._queue.get().get_read_fd()
            if fd < 0:
                self._queue.reset()
                return False

            # Duplicate the fd so the Python socket object does not own the fd
            # that is already owned by the C++ SockDispatchQueue destructor.
            self._drain_sock = _socket_module.fromfd(
                fd, _socket_module.AF_INET, _socket_module.SOCK_STREAM)
            self._drain_sock.setblocking(False)

            self._dispatch_callables = {}
            self._mode               = _AIO_MGR_PROACTOR
            self._drain_task         = loop.create_task(
                self._proactor_drain_loop(),
                name="nng-proactor-drain",
            )
            return True
        except Exception:
            self._queue.reset()
            if self._drain_sock is not None:
                try:
                    self._drain_sock.close()
                except Exception:
                    pass
                self._drain_sock = None
            self._dispatch_callables = None
            return False

    # ── AIO creation ───────────────────────────────────────────────────────

    cdef _AioSockASync create_aio_for_socket(self, shared_ptr[SocketHandle] anchor):
        """Return a new AIO op anchored to *anchor* (a socket handle).

        Uses the POLL or PROACTOR path when available; falls back to
        :class:`_AioCbASync` otherwise.

        Args:
            anchor: Shared ownership handle for the nng socket.

        Returns:
            A ready-to-submit :class:`_AioSockASync` (or subclass) instance.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        if self._mode == _AIO_MGR_POLL:
            return _AioSockASync.create_for_socket(
                anchor, self._queue, self._dispatch_callables)
        if self._mode == _AIO_MGR_PROACTOR and not self._task_user_cancelled:
            return _AioSockASync.create_for_socket(
                anchor, self._queue, self._dispatch_callables)
        return _AioCbASync.create_for_socket_cb(anchor)

    cdef _AioSockASync create_aio_for_context(self, shared_ptr[ContextHandle] anchor):
        """Return a new AIO op anchored to *anchor* (a context handle).

        Args:
            anchor: Shared ownership handle for the nng context.

        Returns:
            A ready-to-submit :class:`_AioSockASync` (or subclass) instance.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        if self._mode == _AIO_MGR_POLL:
            return _AioSockASync.create_for_context(
                anchor, self._queue, self._dispatch_callables)
        if self._mode == _AIO_MGR_PROACTOR and not self._task_user_cancelled:
            return _AioSockASync.create_for_context(
                anchor, self._queue, self._dispatch_callables)
        return _AioCbASync.create_for_context_cb(anchor)

    # ── Poll-mode readable callback ────────────────────────────────────────

    def _poll_readable_callback(self):
        """Drain the wakeup pipe and dispatch all pending AIO completions.

        Called by the event loop when the :class:`PollDispatchQueue` read fd
        becomes readable.  Runs entirely on the event-loop thread, so
        completion handlers resolve futures directly (no cross-thread hop).
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        cdef deque[uint64_t] ready

        # Drain every wakeup byte so the fd is no longer readable.
        self._queue.get().drain_wakeup()

        # Collect all pending completions under a single mutex acquisition.
        self._queue.get().drain_ready(ready)

        # If we are releasing and the dispatch callables dict has been cleared, skip dispatching.
        if self._dispatch_callables is None:
            return

        # Retrieve all callbacks to call
        callbacks = []
        for op_id in ready:
            cb = self._dispatch_callables.pop(op_id, None)
            if cb is not None:
                callbacks.append(cb)

        lock.unlock()

        # Dispatch in FIFO order. All callbacks run on the loop thread.
        for cb in callbacks:
            try:
                cb()
            except BaseException:
                _print_exc()

    # ── Proactor-mode drain task ───────────────────────────────────────────

    async def _proactor_drain_loop(self):
        """Drain the wakeup socket and dispatch AIO completions forever.

        Runs as a permanent :class:`asyncio.Task` on the
        :class:`~asyncio.ProactorEventLoop`.  Uses ``loop.sock_recv`` to
        block until at least one wakeup byte has been written by the nng
        trampoline, drains any additional bytes that arrived concurrently,
        then dispatches all pending completion callbacks.

        If this task is cancelled by user code (rather than via
        :meth:`release`), ``_task_user_cancelled`` is set so that subsequent
        operations transparently fall back to :class:`_AioCbASync`.
        """
        cdef unique_lock[DCGMutex] lock
        cdef deque[uint64_t] ready

        cdef uint64_t op_id = 0
        try:
            while True:
                # Release the lock while waiting for the wakeup to avoid blocking
                lock_gil_friendly(lock, self._lock)
                loop = self._loop
                drain_sock = self._drain_sock
                lock.unlock()

                # Wait for at least one wakeup byte from the nng trampoline.
                await loop.sock_recv(drain_sock, 32)

                # Re-acquire the lock to access shared state and dispatch callbacks.
                lock_gil_friendly(lock, self._lock)

                # Drain any additional wakeup bytes that arrived concurrently.
                self._queue.get().drain_wakeup()
    

                # Collect all pending completions under a single mutex acquisition.
                self._queue.get().drain_ready(ready)

                # If we are releasing and the dispatch callables dict has been cleared, skip dispatching.
                if self._dispatch_callables is None:
                    return

                # Retrieve all callbacks to call
                callbacks = []
                for op_id in ready:
                    cb = self._dispatch_callables.pop(op_id, None)
                    if cb is not None:
                        callbacks.append(cb)
                ready.clear()

                lock.unlock()

                # Dispatch all pending completions.
                for cb in callbacks:
                    try:
                        cb()
                    except BaseException:
                        _print_exc()

        except _CancelledError:
            if not self._releasing:
                # The task was cancelled by user code; switch to fallback mode.
                self._task_user_cancelled = True
            raise

    # ── Release ────────────────────────────────────────────────────────────

    def release(self):
        """Detach from the event loop; safe to call from any thread.

        Schedules ``loop.remove_reader`` (POLL) or task cancellation
        (PROACTOR) via ``call_soon_threadsafe``, then drops all strong
        references held by this manager.  After this call the manager must
        not be used to create new AIO operations.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        loop = self._loop
        if loop is not None and not loop.is_closed():
            # Schedule mode-specific cleanup on the event-loop thread.
            if self._mode == _AIO_MGR_POLL and self._read_fd >= 0:
                try:
                    loop.call_soon_threadsafe(loop.remove_reader, self._read_fd)
                except Exception:
                    pass
            elif self._mode == _AIO_MGR_PROACTOR:
                task = self._drain_task
                if task is not None and not task.done():
                    self._releasing = True
                    try:
                        loop.call_soon_threadsafe(task.cancel)
                    except Exception:
                        pass

        # Drain the dispatch queue to ensure no callbacks are still pending on the loop thread.
        # Dispatch all pending completions.
        # Note nng_sock_close has spawned all aio callbacks, but they
        # may not have fired yet. Since they own the reference to their
        # dispatch queue, we can release without waiting for them though.
        dispatch_callables = list(self._dispatch_callables.values()) \
            if self._dispatch_callables is not None else []

        # Drop strong references; C++ objects are cleaned up by shared_ptr.
        self._loop       = None
        self._drain_task = None
        self._dispatch_callables = None

        lock.unlock()

        # Call all the callbacks to resolve the futures
        for cb in dispatch_callables:
            if cb is not None:
                try:
                    cb()
                except BaseException:
                    _print_exc()

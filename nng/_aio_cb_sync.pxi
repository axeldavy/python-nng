# nng/_aio_cb_sync.pxi – included into _nng.pyx
#
# _AioCbSync – internal class managing one in-flight async nng operation,
# with a callback trampoline and concurrent Future resolution.
#
# Callback flow (two-part trampoline):
#
#   Part 1 – _aio_trampoline()   (nng thread, NO GIL)
#     • mark_callback_thread() so AioHandle can choose reap vs free
#     • push id(op) to _DISPATCH_QUEUE  ← only thing done here
#
#   Part 2 – _AioCbSync._dispatch_complete()   (dispatcher thread, GIL held)
#     • nng_aio_result() to get the error code
#     • resolve concurrent.futures.Future via set_result/set_exception()

import asyncio as _asyncio
import concurrent.futures as _concurrent_futures

cimport cython

# ── Part 1: GIL-free trampoline ───────────────────────────────────────────────

cdef void _aio_cb_sync_trampoline(void *arg) noexcept nogil:
    """Called by nng's thread pool on op completion – no GIL, no Python."""
    # Push only the object address as an integer; no Python state touched.
    _DISPATCH_QUEUE.push(<uint64_t><void*>arg)

# ── _AioCbSync ────────────────────────────────────────────────────────────────────

@cython.final
cdef class _AioCbSync:
    """One in-flight async nng operation."""

    cdef unique_ptr[AioHandle] _handle
    cdef object _future      # concurrent.futures.Future
    cdef bint   _has_result  # False: return None, True: return Message

    def __cinit__(self):
        # _handle is default-constructed (empty) by Cython's C++ member glue.
        self._future      = None
        self._has_result  = False

    def __init__(self):
        raise RuntimeError("_AioCbSync cannot be instantiated directly")

    def __dealloc__(self):
        self._future  = None

    # ── Factory methods ───────────────────────────────────────────────────

    @staticmethod
    cdef _AioCbSync create_for_socket_concurrent(shared_ptr[SocketHandle] anchor,
                                                 bint has_result):
        """Allocate a _AioCbSync for a socket op returning a concurrent.futures.Future.
        """
        check_nng_init()
        cdef _AioCbSync op = _AioCbSync.__new__(_AioCbSync)

        # Initialize the future
        op._has_result = has_result
        op._future = _concurrent_futures.Future()
        op._future.add_done_callback(op._on_cancel)

        # Initialize the aio
        cdef nng_aio *raw_aio = NULL
        check_err(nng_aio_alloc(&raw_aio, _aio_cb_sync_trampoline, <void *>op))
        op._handle = make_unique[AioHandle](raw_aio, anchor)
        return op

    @staticmethod
    cdef _AioCbSync create_for_context_concurrent(shared_ptr[ContextHandle] anchor,
                                                  bint has_result):
        """Allocate a _AioCbSync for a context op returning a concurrent.futures.Future.
        """
        check_nng_init()
        cdef _AioCbSync op = _AioCbSync.__new__(_AioCbSync)

        # Initialize the future
        op._has_result = has_result
        op._future = _concurrent_futures.Future()
        op._future.add_done_callback(op._on_cancel)

        # Initialize the aio
        cdef nng_aio *raw_aio = NULL
        check_err(nng_aio_alloc(&raw_aio, _aio_cb_sync_trampoline, <void *>op))
        op._handle = make_unique[AioHandle](raw_aio, anchor)
        return op

    # ── Cancellation bridge ───────────────────────────────────────────────

    def _on_cancel(self, fut):
        """Done-callback registered on a concurrent.futures.Future.

        Fires on any terminal state; only acts when the future was cancelled
        so that the underlying nng aio is also cancelled.
        """
        if fut.cancelled():
            self.on_future_cancel()

    # ── Part 2: dispatcher-thread completion handler ──────────────────────

    def _dispatch_complete(self):
        """Resolve the future; called by the dispatcher thread (GIL held).

        nng_aio_result() is safe to call here: by the time the dispatcher runs
        this, the callback has already returned and the result is stable.
        """
        cdef int err
        cdef nng_msg *recv_msg_ptr

        fut  = self._future
        self._future  = None

        if fut is None:
            return

        err = nng_aio_result(self._handle.get().get())

        # concurrent.futures: set_result/set_exception are thread-safe.
        if not fut.done():
            if err != NNG_OK:
                fut.set_exception(_err_from_code(err))
            elif not self._has_result:
                fut.set_result(None)
            else:
                recv_msg_ptr = nng_aio_get_msg(self._handle.get().get())
                if recv_msg_ptr == NULL:
                    fut.set_exception(
                        NngError(NNG_EINTERNAL, "No message in AIO after recv"))
                fut.set_result(Message._from_ptr(recv_msg_ptr))

    # ── Pre-submission helpers ────────────────────────────────────────────

    cdef inline void set_timeout(self, int32_t ms) noexcept nogil:
        """Set the timeout for this operation (in milliseconds)."""
        nng_aio_set_timeout(self._handle.get().get(), ms)

    cdef void set_msg(self, nng_msg *ptr) noexcept nogil:
        """Attach an nng_msg to the underlying aio (for send operations)."""
        nng_aio_set_msg(self._handle.get().get(), ptr)

    # ── Post-completion helpers ───────────────────────────────────────────

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
        global _DISPATCH_CALLABLES

        # Register the dispatch callable keyed by this object's address.
        _DISPATCH_CALLABLES[id(self)] = self._dispatch_complete
        return 0

    cdef int submit_socket_send(self, nng_socket sock):
        """Submit this op as a socket send."""
        self._prepare_submit()
        with nogil:
            nng_socket_send(sock, self._handle.get().get())
        return 0

    cdef int submit_socket_recv(self, nng_socket sock):
        """Submit this op as a socket receive."""
        self._prepare_submit()
        with nogil:
            nng_socket_recv(sock, self._handle.get().get())
        return 0

    cdef int submit_ctx_send(self, nng_ctx ctx):
        """Submit this op as a context send."""
        self._prepare_submit()
        with nogil:
            nng_ctx_send(ctx, self._handle.get().get())
        return 0

    cdef int submit_ctx_recv(self, nng_ctx ctx):
        """Submit this op as a context receive."""
        self._prepare_submit()
        with nogil:
            nng_ctx_recv(ctx, self._handle.get().get())
        return 0

    cdef object get_future(self):
        """Return the Future associated with this operation.

        Retrieve before submission; after submission the field may be cleared
        by the dispatcher at any time.
        """
        return self._future

    cdef int on_future_cancel(self):
        """Cancel the in-flight operation when the caller's Future is cancelled."""
        with nogil:
            nng_aio_cancel(self._handle.get().get())
            # Wait so that on_future_cancel() returns only after the trampoline
            # has fired (and thus the id is already in the dispatch queue).
            nng_aio_wait(self._handle.get().get())
        return 0

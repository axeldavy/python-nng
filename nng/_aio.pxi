# nng/_aio.pxi – included into _nng.pyx
#
# _AioOp – internal class managing one in-flight async nng operation.

import asyncio as _asyncio
from traceback import print_exc as _print_exc

from cpython.ref cimport Py_INCREF, Py_DECREF
cimport cython

cdef extern from "<thread>" nogil:
    cdef cppclass thread_id "std::thread::id":
        bint operator==(thread_id)
        bint operator!=(thread_id)

    thread_id get_id "std::this_thread::get_id"()

cdef void _aio_trampoline(void *arg) noexcept nogil:
    """Called by nng's internal thread pool when an async operation finishes."""
    (<_AioOp>arg).on_complete()

def _set_future_done(fut):
    """Helper to set an asyncio Future as done from the trampoline."""
    if not fut.done():
        fut.set_result(None)

def _set_future_exception(fut, exc):
    """Helper to set an asyncio Future exception from the trampoline."""
    if not fut.done():
        fut.set_exception(exc)

@cython.final
cdef class _AioOp:
    """One in-flight async nng operation."""

    cdef nng_aio *_aio
    cdef thread_id _callback_thread_id
    cdef object _loop     # asyncio event loop
    cdef object _future   # asyncio.Future
    cdef object _payload  # keep Message / bytes alive during the operation

    def __cinit__(self):
        self._aio     = NULL
        self._loop    = None
        self._future  = None
        self._payload = None

    def __init__(self):
        raise RuntimeError("_AioOp cannot be instantiated directly")

    def __dealloc__(self):
        self._future = None
        self._loop = None
        self._payload = None

        if self._aio != NULL:
            # op is no longer in-flight; nng_aio_stop is a no-op here
            if self._callback_thread_id == get_id():
                # We're in the callback thread, we cannot free.
                nng_aio_reap(self._aio)
            else:
                # NOTE: we must rely on the callback to have
                # been completed if the task was submitted.
                # We cannot call nng_aio_stop as it would
                # deadlock on the gil in a separate thread.

                # We are safe to free directly.
                nng_aio_free(self._aio)
            self._aio = NULL

    @staticmethod
    cdef _AioOp create():
        """Allocate a new _AioOp with async infrastructure wired up."""
        check_nng_init()
        cdef _AioOp op = _AioOp.__new__(_AioOp)

        # Fetch the current event loop
        op._loop   = _asyncio.get_running_loop()

        # Check loop exists
        if op._loop is None:
            raise RuntimeError("No running event loop found")

        # Create future
        op._future = op._loop.create_future()

        # Allocate the nng_aio with our trampoline and the op as context
        check_err(nng_aio_alloc(&op._aio, _aio_trampoline, <void *> op))

        # NOTE: we could add an add_done_callback to the future
        # to handle cancellation (nng_aio_cancel + nng_aio_wait) if needed.

        return op

    cdef void on_complete(self) noexcept nogil:
        """Resolve the asyncio Future from any thread (called with GIL held)."""
        # Retrieve callback thread id
        self._callback_thread_id = get_id()

        # Retrieve operation completion status
        cdef int err = nng_aio_result(self._aio)

        with gil:
            try:
                # Clear references to loop, future, and payload
                fut = self._future
                loop = self._loop
                self._future = None
                self._loop = None
                self._payload = None

                # Check item is still valid (interpreted exit)
                if loop is None or fut is None:
                    return

                # Report completion
                if err == NNG_OK:
                    loop.call_soon_threadsafe(_set_future_done, fut)
                else:
                    exc = _err_from_code(err)
                    loop.call_soon_threadsafe(_set_future_exception, fut, exc)

            # Print any unexpected exceptions but don't let them propagate out of the trampoline
            except BaseException:
                _print_exc()

            # Clear callback reference
            finally:
                Py_DECREF(self)

    ## Editing the aio before submission

    cdef inline void set_timeout(self, int32_t ms) noexcept nogil:
        """Set the timeout for this operation (in milliseconds)."""
        nng_aio_set_timeout(self._aio, ms)

    cdef void set_payload(self, object payload) noexcept:
        """Keep *payload* (Message / bytes) alive for the duration of the op."""
        self._payload = payload

    cdef void set_msg(self, nng_msg *ptr) noexcept nogil:
        """Attach an nng_msg to the underlying aio (for send operations)."""
        nng_aio_set_msg(self._aio, ptr)

    ## Retrieving results from the aio after completion

    cdef nng_msg *get_msg(self) noexcept nogil:
        """Retrieve the received nng_msg from the aio (for recv operations).

        Returns NULL if no message is present.
        """
        return nng_aio_get_msg(self._aio)

    ## aio submission

    cdef void _prepare_submit(self):
        """Mark as in-flight (must call immediately before the nng op).
        
        Must be called just before submission
        """
        # NOTE: we may use a weakref in the trampoline instead
        Py_INCREF(self)

    cdef void _cancel_submit(self):
        """Unmark as in-flight (must call if the op is cancelled before submission)."""
        Py_DECREF(self)

    cdef void submit_socket_send(self, nng_socket sock):
        """Submit this op as a socket send."""
        self._prepare_submit()
        with nogil:
            nng_socket_send(sock, self._aio)

    cdef void submit_socket_recv(self, nng_socket sock):
        """Submit this op as a socket receive."""
        self._prepare_submit()
        with nogil:
            nng_socket_recv(sock, self._aio)

    cdef void submit_ctx_send(self, nng_ctx ctx):
        """Submit this op as a context send."""
        self._prepare_submit()
        with nogil:
            nng_ctx_send(ctx, self._aio)

    cdef void submit_ctx_recv(self, nng_ctx ctx):
        """Submit this op as a context receive."""
        self._prepare_submit()
        with nogil:
            nng_ctx_recv(ctx, self._aio)

    cdef object get_future(self):
        """Return the asyncio Future associated with this operation.
        
        May return None if the callback has already been called
        and references cleared, in which case the operation is already complete.
        Thus call before submission.
        """
        return self._future

    cdef void on_future_cancel(self):
        """Cancel the in-flight operation (must call if the Future is cancelled)."""
        with nogil:
            nng_aio_cancel(self._aio)

            # Wait for cancellation to complete before returning
            # While this does not seem required, it is probably
            # safer.
            nng_aio_wait(self._aio)

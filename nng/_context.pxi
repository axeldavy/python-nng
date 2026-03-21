# nng/_context.pxi – included into _nng.pyx
#
# Context: wraps nng_ctx for concurrent request-reply without raw sockets.
#
# Supported by: req0, rep0, surveyor0, respondent0. Not all protocols.

cdef class Context:
    """A protocol context (nng_ctx).

    Provides an independent request/reply state machine on a shared socket,
    allowing many concurrent in-flight requests without raw mode::

        sock = ReqSocket()
        sock.add_dialer("tcp://127.0.0.1:5000").start()
        ctx  = sock.open_context()
        ctx.send_msg(Message(b"ping"))
        reply = ctx.recv_msg()
        ctx.close() # optional
    """

    cdef shared_ptr[ContextHandle] _handle
    cdef object _weak_socket_ref

    cdef object __weakref__

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.

    def __init__(self):
        raise RuntimeError("Context cannot be instantiated directly. Use Socket.open_context() instead.")

    @staticmethod
    cdef Context create(shared_ptr[ContextHandle] ch):
        """Take ownership of an already-created ContextHandle."""
        cdef Context context = Context.__new__(Context)
        context._handle = ch
        return context

    @staticmethod
    cdef Context open(Socket socket):
        """Open a new context on *socket* and return a Context wrapping it."""
        cdef int err = 0
        cdef shared_ptr[ContextHandle] ch = make_context(socket._handle, err)
        check_err(err)
        cdef Context ctx = Context.create(ch)
        ctx._weak_socket_ref = _weakref(socket)
        return ctx

    cdef inline void _check(self) except *:
        if not self._handle or not self._handle.get().is_open():
            raise NngClosed(NNG_ECLOSED, "Context is closed")

    def close(self) -> None:
        if self._handle and self._handle.get().is_open():
            check_err(self._handle.get().close())

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @property
    def id(self) -> int:
        return nng_ctx_id(self._handle.get().raw())

    @property
    def socket(self) -> Socket:
        """The parent Socket for this context."""
        self._check()
        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is None:
            raise NngClosed(NNG_ECLOSED, "Parent socket has been closed")
        return sock

    # ── Synchronous send / recv ────────────────────────────────────────────

    def send(self, data, *, bint nonblock = False) -> None:
        """Send a Message through this context (transfers ownership)."""
        self._check()

        # Build the nng message
        cdef bint message_is_copy = False
        cdef nng_msg *ptr = NULL
        if not isinstance(data, Message):
            check_err(nng_msg_from_data(data, &ptr))
            message_is_copy = True
        else:
            ptr = (<Message>data)._steal_ptr()

        # Fast path: non-blocking send delegates to nng_ctx_sendmsg directly.
        cdef int rv
        if nonblock:
            with nogil:
                rv = nng_ctx_sendmsg(self._handle.get().raw(), ptr, NNG_FLAG_NONBLOCK)
            if rv != 0:
                if message_is_copy:
                    nng_msg_free(ptr)
                else:
                    (<Message>data)._handle.restore(ptr)
                check_err(rv)
            return

        # Blocking path: use an AIO and poll every 100 ms so that
        # Python signals (Ctrl-C) are processed between polls.
        cdef bint still_busy

        # Allocate aio
        cdef nng_aio *aio = NULL
        check_err(nng_aio_alloc(&aio, NULL, NULL))

        try:
            # Configure aio
            nng_aio_set_timeout(aio, NNG_DURATION_DEFAULT)
            nng_aio_set_msg(aio, ptr)

            # Submit the send and do the first wait
            with nogil:
                nng_ctx_send(self._handle.get().raw(), aio)
                still_busy = nng_aio_wait_until(aio, nng_clock() + 100)

            # If the aio completed, return
            if not still_busy:
                rv = nng_aio_result(aio)
                if rv != 0:
                    if message_is_copy:
                        nng_msg_free(ptr)
                    else:
                        (<Message>data)._handle.restore(ptr)
                    check_err(rv)
                return

            # Wait with periodic signal checks; cancels AIO on Ctrl-C and raises.
            try:
                while still_busy:
                    PyErr_CheckSignals()
                    with nogil:
                        still_busy = nng_aio_wait_until(aio, nng_clock() + 100)
            except BaseException:
                # We cancel and ensure completion to have valid aio result to check.
                # the wait should be fast since we are cancelling.
                if nng_aio_busy(aio):
                    nng_aio_cancel(aio)
                    nng_aio_wait(aio)
                raise
            finally:
                rv = nng_aio_result(aio)
                # nng only consumes the message on success.
                if rv != 0:
                    if message_is_copy:
                        nng_msg_free(ptr)
                    else:
                        (<Message>data)._handle.restore(ptr)

            # Raise if the send failed.
            check_err(rv)
        finally:
            nng_aio_free(aio)

    def recv(self, *, bint nonblock = False) -> Message:
        """Receive a Message through this context."""
        self._check()

        # Fast path: non-blocking recv delegates to nng_ctx_recvmsg directly.
        cdef int rv
        cdef nng_msg *ptr = NULL
        if nonblock:
            with nogil:
                rv = nng_ctx_recvmsg(self._handle.get().raw(), &ptr, NNG_FLAG_NONBLOCK)
            check_err(rv)
            return Message._from_ptr(ptr)

        # Blocking path: use an AIO and poll every 100 ms so that
        # Python signals (Ctrl-C) are processed between polls.
        cdef bint still_busy

        # Allocate aio
        cdef nng_aio *aio = NULL
        check_err(nng_aio_alloc(&aio, NULL, NULL))
        try:
            nng_aio_set_timeout(aio, NNG_DURATION_DEFAULT)

            # Submit the recv and do the first wait
            with nogil:
                nng_ctx_recv(self._handle.get().raw(), aio)
                still_busy = nng_aio_wait_until(aio, nng_clock() + 100)

            # If the aio completed, return
            if not still_busy:
                rv = nng_aio_result(aio)

                # Raise on error
                check_err(rv)

                # Else return the message
                ptr = nng_aio_get_msg(aio)
                return Message._from_ptr(ptr)

            # Wait with periodic signal checks; cancels AIO on Ctrl-C and raises.
            try:
                while still_busy:
                    PyErr_CheckSignals()
                    with nogil:
                        still_busy = nng_aio_wait_until(aio, nng_clock() + 100)
            except BaseException:
                # We cancel and ensure completion to have valid aio result to check.
                # the wait should be fast since we are cancelling.
                if nng_aio_busy(aio):
                    nng_aio_cancel(aio)
                    nng_aio_wait(aio)

                # If the AIO completed before cancellation, a received message is
                # owned by us and must be freed (rv == 0 means message is present).
                rv = nng_aio_result(aio)
                if rv == 0:
                    nng_msg_free(nng_aio_get_msg(aio))

                # Now we can safely raise the signal exception.
                raise

            # If we are here, the AIO completed without Python signals; retrieve the message and return it.
            rv = nng_aio_result(aio)

            # Raise on error
            check_err(rv)

            # Else return the message
            ptr = nng_aio_get_msg(aio)
            return Message._from_ptr(ptr)
        finally:
            nng_aio_free(aio)


    # ── Async send / recv ─────────────────────────────────────────────────

    async def asend(self, data) -> None:
        """Send a Message asynchronously through this context."""
        self._check()

        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None

        if sock is None:
            raise RuntimeError("Invalid Socket")

        # Obtain the per-loop AIO manager (created lazily on first call).
        cdef _AioAsyncManager mgr = (<Socket>sock).get_aio_manager()
        cdef _AioSockASync op = mgr.create_aio_for_context(self._handle)
        del sock

        # Build the nng message
        cdef nng_msg *ptr = NULL
        if not isinstance(data, Message):
            check_err(nng_msg_from_data(data, &ptr))
        else:
            ptr = (<Message>data)._steal_ptr()

        # Transfer message ownership
        op.set_msg(ptr)

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        cdef bint completed = op.submit_ctx_send(self._handle.get().raw())

        # Fast-path: if the AIO is already complete (if rep could be received immediately))
        if completed:
            if nng_aio_busy(op._handle.get().get()):
                nng_aio_wait(op._handle.get().get())
            check_err(nng_aio_result(op._handle.get().get()))
            return

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise
        finally:
            op.ensure_finish()

    async def arecv(self):
        """Receive a Message asynchronously through this context."""
        self._check()

        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None

        if sock is None:
            raise RuntimeError("Invalid Socket")

        # Obtain the per-loop AIO manager (created lazily on first call).
        cdef _AioAsyncManager mgr = (<Socket>sock).get_aio_manager()
        cdef _AioSockASync op = mgr.create_aio_for_context(self._handle)
        del sock

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        cdef bint completed = op.submit_ctx_recv(self._handle.get().raw())

        # Fast-path: if the AIO is already complete (if rep could be received immediately))
        if completed:
            if nng_aio_busy(op._handle.get().get()):
                nng_aio_wait(op._handle.get().get())
            check_err(nng_aio_result(op._handle.get().get()))

        else:
            # Await completion
            try:
                await future
            except _asyncio.CancelledError:
                op.on_future_cancel()
                raise
            finally:
                op.ensure_finish()

        # Retrieve the message from the completed operation
        cdef nng_msg *ptr = op.get_msg()
        if ptr == NULL:
            raise NngError(NNG_EINTERNAL, "No message in AIO after recv")

        # Take ownership of the message and return it
        return Message._from_ptr(ptr)


    # ── concurrent.futures submit helpers ─────────────────────────────────

    def submit_send(self, data) -> _concurrent_futures.Future:
        """Send *data* (bytes-like) asynchronously; return a concurrent.futures.Future.

        The future resolves to ``None`` on success or raises an :exc:`NngError`
        on failure.  Can be called from any thread without a running event loop.
        """
        self._check()

        # Create an aio request
        cdef _AioCbSync op = _AioCbSync.create_for_context_concurrent(self._handle, False)

        # Build the nng message
        cdef nng_msg *ptr = NULL
        if not isinstance(data, Message):
            check_err(nng_msg_from_data(data, &ptr))
        else:
            ptr = (<Message>data)._steal_ptr()

        # Transfer message ownership
        op.set_msg(ptr)

        # Fetch the future now as get_future is invalid after submission
        fut = op.get_future()

        # Submit the operation
        op.submit_ctx_send(self._handle.get().raw())

        # Return the concurrent.futures.Future to the caller
        return fut

    def submit_recv(self) -> _concurrent_futures.Future:
        """Receive asynchronously; return a concurrent.futures.Future → bytes.

        The future resolves to the received payload as ``bytes``.
        Can be called from any thread without a running event loop.
        """
        self._check()

        # Create an aio request
        cdef _AioCbSync op = _AioCbSync.create_for_context_concurrent(self._handle, True)

        # Fetch the future now as get_future is invalid after submission
        fut = op.get_future()

        # Submit the operation
        op.submit_ctx_recv(self._handle.get().raw())

        # Return the concurrent.futures.Future to the caller
        return fut

    # ── Options ───────────────────────────────────────────────────────────

    @property
    def recv_timeout(self) -> int:
        """Receive timeout in ms (-1 = infinite, 0 = non-blocking)."""
        self._check()
        cdef nng_duration v
        check_err(nng_ctx_get_ms(self._handle.get().raw(), b"recv-timeout", &v))
        return v

    @recv_timeout.setter
    def recv_timeout(self, int ms):
        self._check()
        check_err(nng_ctx_set_ms(self._handle.get().raw(), b"recv-timeout", ms))

    @property
    def send_timeout(self) -> int:
        """Send timeout in ms."""
        self._check()
        cdef nng_duration v
        check_err(nng_ctx_get_ms(self._handle.get().raw(), b"send-timeout", &v))
        return v

    @send_timeout.setter
    def send_timeout(self, int ms):
        self._check()
        check_err(nng_ctx_set_ms(self._handle.get().raw(), b"send-timeout", ms))

    def __repr__(self) -> str:
        cdef bint is_open = self._handle.get() != NULL and self._handle.get().is_open()
        return f"Context(id={nng_ctx_id(self._handle.get().raw()) if is_open else 0}, open={is_open})"

cdef class SubContext(Context):
    """Independent sub context on a :class:`SubSocket`.

    Sub contexts have an independant subscription state as their
    parent socket. They thus can be used to implement multiple logical
    subscribers sharing the same underlying socket.
    """

    @staticmethod
    cdef SubContext open(Socket socket):
        """Open a new context on *socket* and return a SubContext wrapping it."""
        cdef int err = 0
        cdef shared_ptr[ContextHandle] ch = make_context(socket._handle, err)
        check_err(err)
        cdef SubContext ctx = SubContext.__new__(SubContext)
        ctx._handle = ch
        return ctx

    def subscribe(self, prefix: bytes = b"") -> None:
        """Subscribe to messages whose body starts with *prefix*.

        An empty prefix subscribes to all messages.
        """
        self._check()
        cdef const unsigned char[::1] mv = prefix
        check_err(nng_sub0_ctx_subscribe(
            self._handle.get().raw(),
            &mv[0] if len(mv) else NULL, len(mv))
        )

    def unsubscribe(self, prefix: bytes = b"") -> None:
        """Unsubscribe from messages whose body starts with *prefix*.

        An empty prefix unsubscribes from all messages.
        """
        self._check()
        cdef const unsigned char[::1] mv = prefix
        check_err(nng_sub0_ctx_unsubscribe(
            self._handle.get().raw(),
            &mv[0] if len(mv) else NULL, len(mv))
        )
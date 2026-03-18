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
        return Context.create(ch)

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

        # Send message
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef int rv
        with nogil:
            rv = nng_ctx_sendmsg(self._handle.get().raw(), ptr, flags)

        # Raise on errors and restore/free message
        if rv != 0:
            if message_is_copy:
                # nng did not take ownership; free the internally created copy.
                nng_msg_free(ptr)
            else:
                # nng did not take ownership; restore it to the Python wrapper.
                (<Message>data)._handle.restore(ptr)
            check_err(rv)

    def recv(self, *, bint nonblock = False) -> Message:
        """Receive a Message through this context."""
        self._check()

        # Receive incoming message
        cdef nng_msg *ptr = NULL
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef int rv
        with nogil:
            rv = nng_ctx_recvmsg(self._handle.get().raw(), &ptr, flags)

        # Raise on error
        check_err(rv)

        # Return result
        return Message._from_ptr(ptr)


    # ── Async send / recv ─────────────────────────────────────────────────

    async def asend(self, data) -> None:
        """Send a Message asynchronously through this context."""
        self._check()

        # See socket asend() for why we cannot try to send
        # with NONBLOCK first and fall back to the async machinery on EAGAIN.

        # Create an aio request
        cdef _AioOp op = _AioOp.create_for_context(self._handle)

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
        op.submit_ctx_send(self._handle.get().raw())

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

        # Fast path: non-blocking recv uses a NULL-callback stack aio inside
        # nng — no task-pool dispatch, no cross-thread wakeups (~200–500 ns).
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_ctx_recvmsg(self._handle.get().raw(), &msg, NNG_FLAG_NONBLOCK)

        # On success the message is received and we can return.
        if rv == NNG_OK:
            return Message._from_ptr(msg)

        # On EAGAIN the message was not received but we can still proceed to the slow path.
        # On any other error the message was not received and we must raise immediately.
        if rv != NNG_EAGAIN:
            check_err(rv)

        # Slow path: recv queue was empty; use the async aio machinery.
        cdef _AioOp op = _AioOp.create_for_context(self._handle)

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        op.submit_ctx_recv(self._handle.get().raw())

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
        cdef _AioOp op = _AioOp.create_for_context_concurrent(self._handle, False)

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
        cdef _AioOp op = _AioOp.create_for_context_concurrent(self._handle, True)

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
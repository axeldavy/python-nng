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

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.

    def __init__(self):
        raise RuntimeError("Context cannot be instantiated directly. Use Socket.open_context() instead.")

    # __dealloc__ intentionally omitted: shared_ptr[ContextHandle] destructor
    # calls ContextHandle::~ContextHandle() -> nng_ctx_close() when refcount hits 0.
    # AioHandle holds a copy of this shared_ptr while a context-level async
    # operation is in flight, ensuring the context outlives the operation.

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

    def send_msg(self, Message msg, *, bint nonblock = False) -> None:
        """Send a Message through this context (transfers ownership)."""
        self._check()

        # Steal the message pointer from the Message object, transferring ownership to nng.
        cdef nng_msg *ptr = msg._steal_ptr()

        # Send message
        cdef int rv
        with nogil:
            rv = nng_ctx_sendmsg(self._handle.get().raw(), ptr, NNG_FLAG_NONBLOCK if nonblock else 0)

        # Handle failure
        if rv != 0:
            msg._handle.restore(ptr)
            check_err(rv)

    def recv_msg(self, *, bint nonblock = False) -> Message:
        """Receive a Message through this context."""
        self._check()

        # Receive any message
        cdef nng_msg *ptr = NULL
        cdef int rv
        with nogil:
            rv = nng_ctx_recvmsg(self._handle.get().raw(), &ptr, NNG_FLAG_NONBLOCK if nonblock else 0)

        # Handle failure
        check_err(rv)

        # Return result
        return Message._from_ptr(ptr)

    def send(self, data, *, bint nonblock = False) -> None:
        """Send raw bytes through this context."""
        self.send_msg(Message(data), nonblock=nonblock)

    def recv(self, *, bint nonblock = False) -> bytes:
        """Receive bytes through this context."""
        return self.recv_msg(nonblock=nonblock).to_bytes()

    # ── Async send / recv ─────────────────────────────────────────────────

    async def asend_msg(self, Message msg) -> None:
        """Send a Message asynchronously through this context."""
        self._check()

        # See socket asend() for explanation
        """
        cdef nng_msg *ptr = msg._steal_ptr()

        # Fast path: non-blocking send uses a NULL-callback stack aio inside
        # nng — no task-pool dispatch, no cross-thread wakeups (~200–500 ns).
        cdef int rv
        with nogil:
            rv = nng_ctx_sendmsg(self._handle.get().raw(), ptr, NNG_FLAG_NONBLOCK)

        # On success the message is sent and we are done.
        if rv == NNG_OK:
            return

        # On EAGAIN the message was not sent but we still own it; we can proceed to the slow path.
        # On any other error the message was not sent; restore ownership to the caller's Message
        # object before raising.
        if rv != NNG_EAGAIN:
            msg._handle.restore(ptr)
            check_err(rv)

        # Slow path: re-wrap the ptr (nng did not take it on EAGAIN).
        msg = Message._from_ptr(ptr)
        """

        cdef _AioOp op = _AioOp.create_for_context(self._handle)
        op.set_payload(msg)
        op.set_msg(msg._steal_ptr())

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

    async def arecv_msg(self):
        """Receive a Message asynchronously through this context."""
        self._check()

        # Fast path: non-blocking recv uses a NULL-callback stack aio inside
        # nng — no task-pool dispatch, no cross-thread wakeups (~200–500 ns).
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_ctx_recvmsg(self._handle.get().raw(), &msg, NNG_FLAG_NONBLOCK)

        # On success the message is received and we can convert it to bytes and return.
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
        return Message._from_ptr(ptr)

    async def asend(self, data) -> None:
        """Send bytes asynchronously through this context."""
        await self.asend_msg(Message(data))

    async def arecv(self) -> bytes:
        """Receive bytes asynchronously through this context."""
        return (await self.arecv_msg()).to_bytes()

    # ── Options ───────────────────────────────────────────────────────────

    @property
    def recv_timeout(self) -> int:
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

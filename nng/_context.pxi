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

    cdef nng_ctx   _ctx
    cdef bint      _open
    cdef object    _socket # to prevent socket from being deallocated

    def __cinit__(self):
        check_nng_init()
        self._open = False
        self._socket = None

    def __init__(self):
        raise RuntimeError("Context cannot be instantiated directly. Use Socket.open_context() instead.")

    def __dealloc__(self):
        if self._open:
            nng_ctx_close(self._ctx)
            self._open = False

    @staticmethod
    cdef Context create(Socket socket):
        cdef Context context = Context.__new__(Context)
        check_err(nng_ctx_open(&context._ctx, socket._sock))
        context._open = True
        context._socket = socket
        return context

    cdef inline void _check(self) except *:
        if not self._open:
            raise NngClosed(NNG_ECLOSED, "Context is closed")

    def close(self) -> None:
        if self._open:
            check_err(nng_ctx_close(self._ctx))
            self._open = False

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @property
    def id(self) -> int:
        return nng_ctx_id(self._ctx)

    # ── Synchronous send / recv ────────────────────────────────────────────

    def send_msg(self, Message msg, *, bint nonblock = False) -> None:
        """Send a Message through this context (transfers ownership)."""
        self._check()

        # Steal the message pointer from the Message object, transferring ownership to nng.
        cdef nng_msg *ptr = msg._steal_ptr()

        # Send message
        cdef int rv
        with nogil:
            rv = nng_ctx_sendmsg(self._ctx, ptr, NNG_FLAG_NONBLOCK if nonblock else 0)

        # Handle failure
        if rv != 0:
            msg._ptr   = ptr
            msg._owned = True
            check_err(rv)

    def recv_msg(self, *, bint nonblock = False) -> Message:
        """Receive a Message through this context."""
        self._check()

        # Receive any message
        cdef nng_msg *ptr = NULL
        cdef int rv
        with nogil:
            rv = nng_ctx_recvmsg(self._ctx, &ptr, NNG_FLAG_NONBLOCK if nonblock else 0)

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
            rv = nng_ctx_sendmsg(self._ctx, ptr, NNG_FLAG_NONBLOCK)

        # On success the message is sent and we are done.
        if rv == NNG_OK:
            return

        # On EAGAIN the message was not sent but we still own it; we can proceed to the slow path.
        # On any other error the message was not sent; restore ownership to the caller's Message
        # object before raising.
        if rv != NNG_EAGAIN:
            msg._ptr   = ptr
            msg._owned = True
            check_err(rv)

        # Slow path: re-wrap the ptr (nng did not take it on EAGAIN).
        msg = Message._from_ptr(ptr)
        """

        cdef _AioOp op = _AioOp.create()
        op.set_payload(msg)
        op.set_msg(msg._steal_ptr())

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        op.submit_ctx_send(self._ctx)

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise

    async def arecv_msg(self):
        """Receive a Message asynchronously through this context."""
        self._check()

        # Fast path: non-blocking recv uses a NULL-callback stack aio inside
        # nng — no task-pool dispatch, no cross-thread wakeups (~200–500 ns).
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_ctx_recvmsg(self._ctx, &msg, NNG_FLAG_NONBLOCK)

        # On success the message is received and we can convert it to bytes and return.
        if rv == NNG_OK:
            return Message._from_ptr(msg)

        # On EAGAIN the message was not received but we can still proceed to the slow path.
        # On any other error the message was not received and we must raise immediately.
        if rv != NNG_EAGAIN:
            check_err(rv)

        # Slow path: recv queue was empty; use the async aio machinery.
        cdef _AioOp op = _AioOp.create()

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        op.submit_ctx_recv(self._ctx)

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise

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

    # ── Poll-fd readiness helpers ─────────────────────────────────────────

    def arecv_ready(self):
        """Async generator that yields each time the context's socket has a message ready.

        Delegates to :meth:`Socket.arecv_ready` on the owning socket — contexts
        share the socket-level poll fds.  See that method for full documentation.
        """
        self._check()
        return (<Socket> self._socket).arecv_ready()

    def asend_ready(self):
        """Async generator that yields each time the context's socket can accept a send.

        Delegates to :meth:`Socket.asend_ready` on the owning socket — contexts
        share the socket-level poll fds.  See that method for full documentation.
        """
        self._check()
        return (<Socket> self._socket).asend_ready()

    # ── Options ───────────────────────────────────────────────────────────

    @property
    def recv_timeout(self) -> int:
        self._check()
        cdef nng_duration v
        check_err(nng_ctx_get_ms(self._ctx, b"recv-timeout", &v))
        return v

    @recv_timeout.setter
    def recv_timeout(self, int ms):
        self._check()
        check_err(nng_ctx_set_ms(self._ctx, b"recv-timeout", ms))

    @property
    def send_timeout(self) -> int:
        self._check()
        cdef nng_duration v
        check_err(nng_ctx_get_ms(self._ctx, b"send-timeout", &v))
        return v

    @send_timeout.setter
    def send_timeout(self, int ms):
        self._check()
        check_err(nng_ctx_set_ms(self._ctx, b"send-timeout", ms))

    def __repr__(self) -> str:
        return f"Context(id={nng_ctx_id(self._ctx)}, open={self._open})"

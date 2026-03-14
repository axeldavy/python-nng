# nng/_socket.pxi – included into _nng.pyx
#
# Socket        – base class, owns nng_socket
# Pipe          – thin (non-owning) wrapper returned by pipe callbacks
# PairSocket    – pair0 / pair1
# PubSocket     – pub0
# SubSocket     – sub0
# ReqSocket     – req0
# RepSocket     – rep0
# PushSocket    – push0
# PullSocket    – pull0
# SurveyorSocket   – surveyor0
# RespondentSocket – respondent0
# BusSocket     – bus0

from cpython.bytes cimport PyBytes_FromStringAndSize
from selectors import DefaultSelector as _DefaultSelector, EVENT_READ as _EVENT_READ

# ── Pipe event trampoline ─────────────────────────────────────────────────────

cdef void _pipe_trampoline(nng_pipe pipe,
                           nng_pipe_ev ev,
                           void *arg) noexcept nogil:
    """Dispatch nng pipe events to registered Python callbacks (no GIL → GIL)."""
    with gil:
        callbacks = <object> arg
        cb = callbacks.get(int(ev))
        if cb is not None:
            try:
                cb(pipe.id, int(ev))
            except Exception:
                pass  # don't propagate exceptions back to nng's C stack


# ── Pipe ──────────────────────────────────────────────────────────────────────

class Pipe:
    """A *pipe* represents a single physical connection on a socket.

    Pipes are created automatically:

    * A :class:`Dialer` creates one pipe when its outbound connection succeeds
      and recreates it transparently after each reconnect.
    * A :class:`Listener` creates one pipe for every inbound connection it
      accepts.

    All pipes from all dialers and listeners on a socket are pooled together.
    The protocol layer (REQ, PUB, PUSH, …) decides how to distribute messages
    across that pool — broadcast, round-robin, load-balancing, etc.

    Applications normally never create ``Pipe`` objects directly.  They appear
    as arguments to callbacks registered with :meth:`Socket.on_pipe_event`,
    allowing the application to inspect or forcibly close individual
    connections.  The ``Pipe`` object is **non-owning** — closing it simply
    kicks the connection; the underlying socket is unaffected.
    """

    __slots__ = ("_id",)

    def __init__(self, uint32_t pipe_id):
        self._id = pipe_id

    @property
    def id(self) -> int:
        return self._id

    def close(self) -> None:
        """Forcibly close this connection.

        This immediately terminates the underlying transport connection.  For
        a dialer-owned pipe, the dialer will attempt to reconnect afterwards.
        For a listener-owned pipe, the remote peer will need to reconnect.
        Any in-flight sends or receives on this pipe fail immediately.
        """
        cdef nng_pipe p
        p.id = self._id
        nng_pipe_close(p)   # ignore error (pipe may already be gone)

    def __repr__(self) -> str:
        return f"Pipe(id={self._id})"


# ── Pipe-event constants (re-exported at module level) ────────────────────────
PIPE_EV_ADD_PRE  = NNG_PIPE_EV_ADD_PRE
PIPE_EV_ADD_POST = NNG_PIPE_EV_ADD_POST
PIPE_EV_REM_POST = NNG_PIPE_EV_REM_POST


# ── Socket base class ─────────────────────────────────────────────────────────

cdef class Socket:
    """Base class for all nng protocol sockets.

    **Connections and pipes**

    A socket does not correspond to a single connection.  Instead it owns a
    *pool of pipes*, where each pipe is one physical connection:

    * Call :meth:`add_dialer` one or more times to add outbound connections.
      Each call creates and stores a :class:`Dialer`; call
      :meth:`Dialer.start` on it to initiate the connection.  A dialer
      manages one pipe and reconnects automatically if the connection drops.
    * Call :meth:`add_listener` one or more times to accept inbound
      connections.  A single :class:`Listener` can accept unlimited
      simultaneous peers; call :meth:`Listener.start` on it to begin
      accepting.
    * Both can be combined freely on the same socket.
    * The socket keeps strong references to all dialers and listeners it
      creates, so they are not garbage-collected while the socket is open.

    **How messages are routed across pipes**

    This is entirely determined by the protocol subclass:

    * :class:`PubSocket` — broadcast to *all* pipes.
    * :class:`PushSocket` — round-robin / load-balance across pipes.
    * :class:`ReqSocket` — send to *one* available pipe; reply must arrive on
      the same pipe.  Uses the first ready pipe, not strict round-robin.
    * :class:`PairSocket` — one active pipe at a time (or multiple in
      polyamorous mode).
    * :class:`BusSocket` — send to *all other* peers.
    * :class:`SurveyorSocket` — broadcast survey; collect all responses.

    **Typical usage**::

        with ReqSocket() as sock:
            sock.add_dialer("tcp://127.0.0.1:5555").start()
            sock.send(b"ping")
            reply = sock.recv()
    """

    cdef nng_socket _sock
    # Dict mapping int(nng_pipe_ev) → Python callable; kept alive as long as
    # the socket is open so the trampoline arg pointer stays valid.
    cdef object _pipe_cbs
    # Strong references to all dialers/listeners created on this socket so
    # they are not garbage-collected while the socket is open.
    cdef list _dialers
    cdef list _listeners

    # For polling. Lazy init.
    cdef int _fd_recv
    cdef int _fd_send

    def __cinit__(self):
        check_nng_init()
        self._sock.id = 0
        self._pipe_cbs = {}
        self._dialers = []
        self._listeners = []
        self._fd_recv = -1
        self._fd_send = -1

    def __dealloc__(self):
        if self._sock.id != 0:
            nng_socket_close(self._sock)   # best-effort in dealloc
            self._sock.id = 0

    cdef inline void _check(self) except *:
        if self._sock.id == 0:
            raise NngClosed(NNG_ECLOSED, "Socket is closed")

    def close(self) -> None:
        """Close the socket and release all resources."""
        if self._sock.id != 0:
            with nogil:
                nng_socket_close(self._sock)
            self._sock.id = 0

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def id(self) -> int:
        """The underlying nng socket ID (positive) or 0 if closed."""
        if self._sock.id == 0:
            return 0
        return nng_socket_id(self._sock)

    @property
    def proto_name(self) -> str:
        """Protocol name (e.g. ``"pair1"``)."""
        self._check()
        cdef const char *name = NULL
        check_err(nng_socket_proto_name(self._sock, &name))
        return name.decode("utf-8")

    @property
    def peer_name(self) -> str:
        """Peer protocol name."""
        self._check()
        cdef const char *name = NULL
        check_err(nng_socket_peer_name(self._sock, &name))
        return name.decode("utf-8")

    # ── Options ───────────────────────────────────────────────────────────

    @property
    def recv_timeout(self) -> int:
        """Receive timeout in ms (-1 = infinite, 0 = non-blocking)."""
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._sock, b"recv-timeout", &v))
        return v

    @recv_timeout.setter
    def recv_timeout(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._sock, b"recv-timeout", ms))

    @property
    def send_timeout(self) -> int:
        """Send timeout in ms."""
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._sock, b"send-timeout", &v))
        return v

    @send_timeout.setter
    def send_timeout(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._sock, b"send-timeout", ms))

    @property
    def recv_buf(self) -> int:
        """Receive buffer depth (number of queued messages)."""
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._sock, b"recv-buffer", &v))
        return v

    @recv_buf.setter
    def recv_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._sock, b"recv-buffer", n))

    @property
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages)."""
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._sock, b"send-buffer", &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._sock, b"send-buffer", n))

    @property
    def recv_max_size(self) -> int:
        """Maximum incoming message size in bytes (0 = no limit)."""
        self._check()
        cdef size_t v
        check_err(nng_socket_get_size(self._sock, b"recv-size-max", &v))
        return v

    @recv_max_size.setter
    def recv_max_size(self, size_t n):
        self._check()
        check_err(nng_socket_set_size(self._sock, b"recv-size-max", n))

    @property
    def recv_fd(self) -> int:
        """Readable file descriptor for external poll / epoll / kqueue.

        Becomes readable when the socket has data to receive.
        Never read or write to this fd directly.
        """
        self._check()
        cdef int fd
        if self._fd_recv == -1:
            check_err(nng_socket_get_recv_poll_fd(self._sock, &fd))
            self._fd_recv = fd
        return self._fd_recv

    @property
    def send_fd(self) -> int:
        """Writable file descriptor for external poll / epoll / kqueue.

        Becomes writable when the socket can accept a send.
        """
        self._check()
        cdef int fd
        if self._fd_send == -1:
            check_err(nng_socket_get_send_poll_fd(self._sock, &fd))
            self._fd_send = fd
        return self._fd_send

    # ── Dialers and Listeners ─────────────────────────────────────────────

    def add_dialer(self, url, *,
                   tls=None,
                   reconnect_min_ms=None,
                   reconnect_max_ms=None,
                   recv_timeout=None,
                   send_timeout=None) -> Dialer:
        """Create, configure, and register an outbound :class:`Dialer` for *url*.

        The dialer is created and all options applied, but the connection is
        **not** started yet.  Call :meth:`Dialer.start` on the returned object
        to initiate the connection::

            dialer = sock.add_dialer("tcp://127.0.0.1:5555",
                                     reconnect_min_ms=100)
            dialer.start()          # blocks until first connection succeeds

            # or as a one-liner:
            sock.add_dialer("tcp://127.0.0.1:5555").start()

        The socket keeps a strong reference to the returned :class:`Dialer`;
        it will not be garbage-collected while the socket is open.

        Each call registers **one** outbound connection slot.  Call
        :meth:`add_dialer` multiple times to connect to several peers; the
        protocol layer pools all resulting pipes and distributes messages
        across them.

        Parameters
        ----------
        url:
            Connection URL, e.g. ``"tcp://127.0.0.1:5555"``.
        tls:
            A :class:`TlsConfig` for TLS connections.  The object must remain
            alive for the lifetime of the dialer.
        reconnect_min_ms:
            Minimum reconnect interval in ms (default: 100).  After a
            disconnect the dialer waits at least this long before retrying.
        reconnect_max_ms:
            Maximum reconnect interval in ms.  Retries use exponential
            back-off capped at this value.  ``0`` (default) means use
            *reconnect_min_ms* as a fixed interval.
        recv_timeout:
            Receive timeout in ms for the pipe created by this dialer
            (overrides the socket-level value).  ``-1`` = infinite,
            ``0`` = non-blocking.
        send_timeout:
            Send timeout in ms for the pipe created by this dialer
            (overrides the socket-level value).
        """
        self._check()
        cdef bytes b = _encode_url(url)
        cdef nng_dialer d
        check_err(nng_dialer_create(&d, self._sock, b))
        try:
            if tls is not None:
                check_err(nng_dialer_set_tls(d, (<TlsConfig?>tls)._get_ptr()))
            if reconnect_min_ms is not None:
                check_err(nng_dialer_set_ms(d, b"reconnect-time-min", reconnect_min_ms))
            if reconnect_max_ms is not None:
                check_err(nng_dialer_set_ms(d, b"reconnect-time-max", reconnect_max_ms))
            if recv_timeout is not None:
                check_err(nng_dialer_set_ms(d, b"recv-timeout", recv_timeout))
            if send_timeout is not None:
                check_err(nng_dialer_set_ms(d, b"send-timeout", send_timeout))
        except:
            nng_dialer_close(d)
            raise
        obj = Dialer._from_id(d)
        self._dialers.append(obj)
        return obj

    def add_listener(self, url, *,
                     tls=None,
                     recv_timeout=None,
                     send_timeout=None) -> Listener:
        """Create, configure, and register an inbound :class:`Listener` for *url*.

        The listener is created and all options applied, but it does **not**
        start accepting connections yet.  Call :meth:`Listener.start` on the
        returned object to begin listening::

            lst = sock.add_listener("tcp://0.0.0.0:5555", recv_timeout=5000)
            lst.start()

            # or as a one-liner:
            sock.add_listener("tcp://0.0.0.0:5555").start()

        The socket keeps a strong reference to the returned :class:`Listener`;
        it will not be garbage-collected while the socket is open.

        A listener accepts **unlimited** simultaneous inbound connections, each
        of which becomes a pipe in the socket's pool.  Call
        :meth:`add_listener` multiple times to accept on several addresses or
        transports at once.

        Parameters
        ----------
        url:
            Address to bind to, e.g. ``"tcp://0.0.0.0:5555"``.
        tls:
            A :class:`TlsConfig` for TLS connections.  The object must remain
            alive for the lifetime of the listener.
        recv_timeout:
            Receive timeout in ms for pipes accepted by this listener
            (overrides the socket-level value).  ``-1`` = infinite,
            ``0`` = non-blocking.
        send_timeout:
            Send timeout in ms for pipes accepted by this listener
            (overrides the socket-level value).
        """
        self._check()
        cdef bytes b = _encode_url(url)
        cdef nng_listener lst
        check_err(nng_listener_create(&lst, self._sock, b))
        try:
            if tls is not None:
                check_err(nng_listener_set_tls(lst, (<TlsConfig?>tls)._get_ptr()))
            if recv_timeout is not None:
                check_err(nng_listener_set_ms(lst, b"recv-timeout", recv_timeout))
            if send_timeout is not None:
                check_err(nng_listener_set_ms(lst, b"send-timeout", send_timeout))
        except:
            nng_listener_close(lst)
            raise
        obj = Listener._from_id(lst)
        self._listeners.append(obj)
        return obj

    # ── Synchronous send / recv ───────────────────────────────────────────

    def send(self, data, *, bint nonblock=False) -> None:
        """Send raw bytes, releasing the GIL while waiting.

        Parameters
        ----------
        data:
            Bytes, bytearray, or any buffer-protocol object.
        nonblock:
            If *True* raise :exc:`NngAgain` instead of blocking.
        """
        self._check()

        # Convert data to bytes
        cdef bytes data_as_bytes = data if not isinstance(data, bytes) else <bytes> data

        # Create a memory view of the data
        cdef const unsigned char[::1] mv = data_as_bytes

        # Send the message (NOTE: this internally uses nng_sendmsg, copying to a fresh message)
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef int rv
        with nogil:
            rv = nng_send(self._sock, &mv[0], len(mv), flags)

        # Raise on errors
        check_err(rv)

    def recv(self, *, bint nonblock=False) -> bytes:
        """Receive the next message body as *bytes*, releasing the GIL.

        Parameters
        ----------
        nonblock:
            If *True* raise :exc:`NngAgain` if no message is ready.
        """
        self._check()

        # Receive the message
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_recvmsg(self._sock, &msg, flags)

        # Raise on errors
        check_err(rv)

        # Convert result to bytes
        cdef size_t n = nng_msg_len(msg)
        cdef bytes result = PyBytes_FromStringAndSize(<char *> nng_msg_body(msg), n)

        # Free message
        nng_msg_free(msg)

        # Return result
        return result

    def send_msg(self, Message msg, *, bint nonblock=False) -> None:
        """Send an :class:`Message`.  Transfers ownership on success.
        
        Parameters
        ----------
        msg:
            The :class:`Message` to send.
        nonblock:
            If *True* raise :exc:`NngAgain` instead of blocking.
        """
        self._check()

        # Retrieve message pointer
        cdef nng_msg *ptr = msg._steal_ptr()

        # Send message blocking
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef int rv
        with nogil:
            rv = nng_sendmsg(self._sock, ptr, flags)

        # Raise on errors and restore message ownership
        if rv != 0:
            msg._ptr = ptr
            msg._owned = True
            check_err(rv)

    def recv_msg(self, *, bint nonblock=False) -> Message:
        """Receive and return an :class:`Message` (zero-copy via buffer protocol).
        
        Parameters
        ----------
        nonblock:
            If *True* raise :exc:`NngAgain` if no message is ready.
        """
        self._check()

        # Receive incoming message
        cdef nng_msg *ptr = NULL
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef int rv
        with nogil:
            rv = nng_recvmsg(self._sock, &ptr, flags)

        # Raise on error
        check_err(rv)

        # Return message
        return Message._from_ptr(ptr)

    # ── Async send / recv ─────────────────────────────────────────────────

    async def asend(self, data) -> None:
        """Send bytes asynchronously (asyncio-compatible)."""
        self._check()

        # Build the nng message
        msg = Message(data)

        # Fast path disabled
        # -> when EAGAIN is returned in REQ mode,
        # NNG REQ mode expects the send to be "lost"
        # and that the REP should issue a new request.
        # (and thus would should receive before sending again)
        # Since that breaks the expected user behaviour
        # of asend not failing for no valid reason, this
        # fast path is disabled.
        """
        cdef nng_msg *ptr = msg._steal_ptr()

        # Fast path: non-blocking send can perform in the same
        # thread, avoiding thread dispatch.
        cdef int rv
        with nogil:
            rv = nng_sendmsg(self._sock, ptr, NNG_FLAG_NONBLOCK)

        # On success the message is sent and we are done.
        if rv == NNG_OK:
            return

        # On EAGAIN the message was not sent but we still own it;
        # we can proceed to the slow path.
        # On any other error the message was not sent and we must free it before raising.
        if rv != NNG_EAGAIN:
            nng_msg_free(ptr)
            check_err(rv)

        # Slow path: lmq was full.  Re-wrap the ptr (still valid — nng never
        # takes ownership on EAGAIN) and hand it to the async aio machinery.
        msg = Message._from_ptr(ptr)
        """

        cdef _AioOp op = _AioOp.create()
        op.set_payload(msg)
        op.set_msg(msg._steal_ptr())

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        op.submit_socket_send(self._sock)

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise

    async def arecv(self) -> bytes:
        """Receive bytes asynchronously."""
        cdef size_t result_len
        cdef bytes result

        self._check()

        # Fast path: non-blocking recv can perform in the same
        # thread, avoiding thread dispatch.
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_recvmsg(self._sock, &msg, NNG_FLAG_NONBLOCK)

        # On success the message is received and we can convert it to bytes and return.
        if rv == NNG_OK:
            result_len = nng_msg_len(msg)
            result = PyBytes_FromStringAndSize(<char *> nng_msg_body(msg), result_len)
            nng_msg_free(msg)
            return result

        # On EAGAIN the message was not received but we can still proceed to the slow path.
        # On any other error the message was not received and we must raise immediately.
        if rv != NNG_EAGAIN:
            check_err(rv)

        # Slow path: recv queue was empty; use the async aio machinery.
        cdef _AioOp op = _AioOp.create()
        future = op.get_future()

        # Submit the operation
        op.submit_socket_recv(self._sock)

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise

        # Retrieve the message from the completed operation
        cdef nng_msg *ptr = op.get_msg()
        if ptr == NULL:
            raise NngError(NNG_EINTERNAL, "No message in AIO after async recv")

        # Extract the message body as bytes and free the message
        result_len = nng_msg_len(ptr)
        result = PyBytes_FromStringAndSize(<char *> nng_msg_body(ptr), result_len)
        nng_msg_free(ptr)
        return result

    async def asend_msg(self, Message msg) -> None:
        """Send a Message asynchronously."""
        self._check()

        # See asend() for the rationale behind the disabled fast path.
        """
        cdef nng_msg *ptr = msg._steal_ptr()

        # Fast path: non-blocking send — no task-pool dispatch.
        cdef int rv
        with nogil:
            rv = nng_sendmsg(self._sock, ptr, NNG_FLAG_NONBLOCK)

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
        op.submit_socket_send(self._sock)

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise

    async def arecv_msg(self):
        """Receive a Message asynchronously."""
        self._check()

        # Fast path: non-blocking recv — no task-pool dispatch.
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_recvmsg(self._sock, &msg, NNG_FLAG_NONBLOCK)
        if rv == NNG_OK:
            return Message._from_ptr(msg)
        if rv != NNG_EAGAIN:
            check_err(rv)

        # Slow path: recv queue was empty; use the async aio machinery.
        cdef _AioOp op = _AioOp.create()
        future = op.get_future()

        # Submit the operation
        op.submit_socket_recv(self._sock)

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise

        # Retrieve the message from the completed operation
        cdef nng_msg *ptr = op.get_msg()
        if ptr == NULL:
            raise NngError(NNG_EINTERNAL, "No message in AIO after async recv")
        return Message._from_ptr(ptr)

    # ── Poll-fd readiness helpers ─────────────────────────────────────────

    async def arecv_ready(self):
        """Async generator that yields each time the socket has a message ready.

        ``recv_fd`` is registered with the event loop *once* for the lifetime of
        the generator — ``add_reader`` / ``remove_reader`` are each called only
        once regardless of how many messages are awaited.  Epoll fires directly
        in the event loop thread with no nng thread pool and no
        ``call_soon_threadsafe`` round-trip.

        After each ``yield``, a call to :meth:`recv` / :meth:`recv_msg` with
        ``nonblock=True`` is guaranteed to succeed immediately::

            watcher = sock.arecv_ready()
            async for _ in watcher:
                data = sock.recv(nonblock=True)
                # or equivalently:
                # await watcher.__anext__()
                # data = sock.recv(nonblock=True)
        """
        self._check()

        # Initialize fd_recv
        cdef int fd
        if self._fd_recv == -1:
            check_err(nng_socket_get_recv_poll_fd(self._sock, &fd))
            self._fd_recv = fd
        else:
            fd = self._fd_recv

        loop = _asyncio.get_running_loop()

        # Pre-create the first future before registering the reader so the
        # callback is never invoked with a stale reference.
        fut = loop.create_future()

        # Have the loop wake when the fd becomes readable.
        def _wakeup():
            nonlocal fut
            if not fut.done():
                fut.set_result(None)

        loop.add_reader(fd, _wakeup)

        # Use a selector to check if the fd is still readable after each wakeup
        selector = _DefaultSelector()
        selector.register(fd, _EVENT_READ)

        try:
            while True:
                # Check if the fd is actually readable; if so, yield
                if selector.select(timeout=0):
                    yield
                    continue

                # Wait collaboratively until the next wakeup, then loop
                # back to check if the fd is actually readable.
                fut = loop.create_future()
                await fut
        finally:
            loop.remove_reader(fd)

    async def asend_ready(self):
        """Async generator that yields each time the socket can accept a send.

        ``send_fd`` is registered with the event loop *once* for the lifetime of
        the generator — ``add_reader`` / ``remove_reader`` are each called only
        once regardless of how many sends are awaited.  Epoll fires directly in
        the event loop thread with no nng thread pool and no
        ``call_soon_threadsafe`` round-trip.

        After each ``yield``, a call to :meth:`send` / :meth:`send_msg` with
        ``nonblock=True`` is guaranteed to succeed immediately.

        For REP sockets the fd becomes readable as soon as nng has finished
        sending the previous reply — making this the ideal low-latency
        synchronisation point before a non-blocking send::

            recv_watcher = sock.arecv_ready()
            send_watcher = sock.asend_ready()
            async for _ in recv_watcher:
                msg = sock.recv(nonblock=True)
                await send_watcher.__anext__()
                sock.send(reply, nonblock=True)
        """
        self._check()

        # Initialize fd_send
        cdef int fd
        if self._fd_send == -1:
            check_err(nng_socket_get_send_poll_fd(self._sock, &fd))
            self._fd_send = fd
        else:
            fd = self._fd_send

        loop = _asyncio.get_running_loop()

        fut = loop.create_future()

        # Have the loop wake when the fd becomes readable.
        # Despite being a "send" fd, readability must be
        # checked, not writability.
        def _wakeup():
            nonlocal fut
            if not fut.done():
                fut.set_result(None)

        loop.add_reader(fd, _wakeup)

        # Use a selector to check if the fd is still readable after each wakeup
        selector = _DefaultSelector()
        selector.register(fd, _EVENT_READ)

        try:
            while True:
                # Check if the fd is actually readable; if so, yield
                if selector.select(timeout=0):
                    yield
                    continue

                # Wait collaboratively until the next wakeup, then loop
                # back to check if the fd is actually readable.
                fut = loop.create_future()
                await fut
        finally:
            loop.remove_reader(fd)

    # ── Pipe callbacks ────────────────────────────────────────────────────

    def on_pipe_event(self, int event, object callback) -> None:
        """Register *callback* to be called when a pipe-lifecycle event fires.

        This is the primary way to observe connections being established or
        dropped on a socket.  Only one callback per event type is kept; calling
        this method again for the same *event* replaces the previous callback.

        Parameters
        ----------
        event:
            One of:

            * :data:`PIPE_EV_ADD_PRE` — called just *before* a new pipe is
              added to the socket.  You may call ``Pipe(pipe_id).close()``
              here to reject the connection.
            * :data:`PIPE_EV_ADD_POST` — called just *after* a new pipe is
              added and ready to carry messages.
            * :data:`PIPE_EV_REM_POST` — called just *after* a pipe has been
              removed (connection closed or lost).
        callback:
            Callable ``(pipe_id: int, event: int) -> None``.

            .. warning::
               This is called from nng's **internal I/O thread**, not from
               your application thread.  Keep it short, non-blocking, and
               thread-safe.  Do not call blocking nng operations (send/recv)
               from within the callback.
        """
        self._check()
        self._pipe_cbs[event] = callback
        # _pipe_cbs dict is kept alive by self; its address is stable as long
        # as we hold the reference.
        check_err(nng_pipe_notify(
            self._sock,
            <nng_pipe_ev> event,
            _pipe_trampoline,
            <void *> self._pipe_cbs))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self._sock.id})"


# ── Helper ────────────────────────────────────────────────────────────────────

cdef inline bytes _encode_url(url):
    if isinstance(url, bytes):
        return <bytes> url
    return (<str> url).encode("utf-8")


# ── Protocol subclasses ───────────────────────────────────────────────────────

cdef class PairSocket(Socket):
    """Bidirectional, point-to-point socket (Pair protocol).

    A pair socket can send and receive freely in both directions.  In standard
    mode it is strictly **one-to-one**: only a single pipe is active at a time.
    Messages sent while no pipe is connected are held in the send buffer.

    **Polyamorous mode** (``poly=True``) relaxes this: many pipes may be
    active simultaneously.  Outbound messages must carry a pipe ID in their
    header (set via :meth:`Message.set_pipe`) to address a specific peer;
    inbound messages carry the pipe ID of the sender.

    Parameters
    ----------
    v0:
        Use the older Pair v0 protocol instead of the default v1.
    poly:
        Enable polyamorous mode (Pair v1 only).
    raw:
        Open in raw mode (advanced; bypasses the normal protocol state machine).
    """

    def __cinit__(self, *, bint v0=False, bint poly=False, bint raw=False):
        if v0:
            check_err(nng_pair0_open_raw(&self._sock) if raw
                      else nng_pair0_open(&self._sock))
        elif poly:
            check_err(nng_pair1_open_poly(&self._sock))
        else:
            check_err(nng_pair1_open_raw(&self._sock) if raw
                      else nng_pair1_open(&self._sock))

    @property
    def polyamorous(self) -> bool:
        """True if the socket is in polyamorous (fan-out) mode."""
        self._check()
        cdef cpp_bool v
        check_err(nng_socket_get_bool(self._sock, b"pair1:polyamorous", &v))
        return bool(v)

    @polyamorous.setter
    def polyamorous(self, bint v):
        self._check()
        check_err(nng_socket_set_bool(self._sock, b"pair1:polyamorous", v))


cdef class PubSocket(Socket):
    """Publish socket — broadcasts every message to **all** connected subscribers.

    A message sent on a ``PubSocket`` is delivered to every :class:`SubSocket`
    peer that has a matching subscription prefix.  Peers with no matching
    subscription silently discard the message.

    * **Fan-out**: one send → copied to all active pipes.
    * **Topic filtering**: filtering happens on the *subscriber* side based on
      the message body prefix (see :meth:`SubSocket.subscribe`).
    * **No back-pressure**: a slow subscriber does not block the publisher;
      messages for that subscriber are dropped when its send buffer is full.
    * PubSocket cannot receive; it is send-only.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_pub0_open_raw(&self._sock) if raw
                  else nng_pub0_open(&self._sock))


cdef class SubSocket(Socket):
    """Subscribe socket — receives messages from a :class:`PubSocket`.

    By default a ``SubSocket`` receives **nothing**.  You must subscribe to at
    least one topic prefix with :meth:`subscribe` before any messages arrive.
    The socket can hold multiple subscriptions simultaneously.

    Messages whose body does *not* start with at least one registered prefix
    are silently dropped by nng before they reach the application.

    * SubSocket is receive-only; :meth:`send` is not available.
    * One ``SubSocket`` can connect to multiple publishers by calling
      :meth:`dial` several times.

    Example::

        sub = SubSocket()
        sub.add_dialer("tcp://127.0.0.1:5556").start()
        sub.subscribe(b"")       # receive every message
        sub.subscribe(b"news.")  # also accept prefix-matched topics
        msg = sub.recv()
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_sub0_open_raw(&self._sock) if raw
                  else nng_sub0_open(&self._sock))

    def subscribe(self, prefix: bytes = b"") -> None:
        """Subscribe to messages whose body starts with *prefix*.

        An empty prefix subscribes to all messages.
        """
        self._check()
        cdef const unsigned char[::1] mv = prefix
        check_err(nng_sub0_socket_subscribe(
            self._sock, &mv[0] if len(mv) else NULL, len(mv)))

    def unsubscribe(self, prefix: bytes = b"") -> None:
        """Remove a topic subscription."""
        self._check()
        cdef const unsigned char[::1] mv = prefix
        check_err(nng_sub0_socket_unsubscribe(
            self._sock, &mv[0] if len(mv) else NULL, len(mv)))


cdef class ReqSocket(Socket):
    """Request socket — sends a request and waits for exactly one reply.

    The REQ/REP protocol enforces a strict send → receive → send → … cycle on
    each context.  Violating this order (e.g. two sends in a row) raises an
    error.

    **Message routing with multiple peers**

    When the socket has several pipes (e.g. after multiple :meth:`dial` calls
    or when connected to a server with multiple workers via contexts), each
    request is sent to the **first available** pipe — whichever peer is ready
    to accept it.  The reply is expected to arrive on the *same* pipe.

    If the pipe dies before the reply arrives, the request can be automatically
    resent on another pipe after :attr:`resend_time` milliseconds.

    **Concurrent requests**

    A bare ``ReqSocket`` allows only one in-flight request at a time.  To
    issue multiple concurrent requests, open independent contexts with
    :meth:`open_context`.  Each context runs its own REQ state machine and can
    be in-flight on a different pipe simultaneously.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_req0_open_raw(&self._sock) if raw
                  else nng_req0_open(&self._sock))

    @property
    def resend_time(self) -> int:
        """How long (ms) to wait for a reply before resending the request.

        If a reply does not arrive within this interval the request is
        retransmitted — potentially on a *different* pipe if the original peer
        has become unavailable.  ``-1`` (the default) disables automatic
        resending.  Setting a value here provides automatic fault-tolerance
        when working with multiple REP peers.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._sock, b"req:resend-time", &v))
        return v

    @resend_time.setter
    def resend_time(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._sock, b"req:resend-time", ms))

    def open_context(self) -> Context:
        """Open an independent REQ context on this socket.

        Each context has its own send → receive → send state machine and can
        have a request in-flight on a different pipe at the same time as other
        contexts.  This is the preferred way to pipeline concurrent requests
        without opening multiple sockets.
        """
        self._check()
        return Context.create(self)


cdef class RepSocket(Socket):
    """Reply socket — receives a request and sends exactly one reply.

    The REP/REQ protocol enforces a strict receive → send → receive → … cycle
    on each context.  Attempting to receive a second request before replying to
    the first raises an error.

    **Serving multiple concurrent clients**

    A single ``RepSocket`` can be connected to any number of request senders
    (via :meth:`listen` or :meth:`dial`).  However, the socket-level
    :meth:`recv` / :meth:`send` cycle handles only **one request at a time**.
    To serve multiple clients concurrently without blocking, open independent
    per-client contexts with :meth:`open_context`; each context carries the
    reply-routing state for one in-flight request.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_rep0_open_raw(&self._sock) if raw
                  else nng_rep0_open(&self._sock))

    def open_context(self) -> Context:
        """Open an independent REP context on this socket.

        Each context can receive one request and send one reply independently
        of all other contexts.  Use this to serve multiple clients concurrently
        from a single socket without a thread per client.
        """
        self._check()
        return Context.create(self)


cdef class PushSocket(Socket):
    """Push socket — distributes messages across connected pull peers (pipeline).

    PUSH/PULL implements a fan-out work queue.  Each message sent on a
    ``PushSocket`` is delivered to **exactly one** :class:`PullSocket` peer,
    chosen by a fair round-robin / load-balancing algorithm that favours peers
    with available buffer space.

    * Messages are never duplicated — each goes to one worker.
    * If there are no connected peers the send blocks (or raises
      :exc:`NngAgain` in non-blocking mode) until a peer is available.
    * PushSocket is send-only.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_push0_open_raw(&self._sock) if raw
                  else nng_push0_open(&self._sock))


cdef class PullSocket(Socket):
    """Pull socket — receives work items distributed by a :class:`PushSocket`.

    Each message arrives from exactly one push sender and is delivered to
    exactly this socket (not duplicated to others).  Multiple ``PullSocket``
    instances connected to the same ``PushSocket`` act as a pool of workers:
    nng load-balances across them automatically.

    * PullSocket is receive-only.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_pull0_open_raw(&self._sock) if raw
                  else nng_pull0_open(&self._sock))


cdef class SurveyorSocket(Socket):
    """Surveyor socket — broadcasts a survey and collects responses within a deadline.

    Sending a message broadcasts it to **all** connected
    :class:`RespondentSocket` peers (like PUB).  The surveyor then receives
    responses from as many respondents as reply within :attr:`survey_time`
    milliseconds.  After the deadline, further receives raise
    :exc:`NngError` with ``NNG_ETIMEDOUT``.

    Each send opens a new survey; the previous survey's results are discarded.
    Use :meth:`open_context` to run multiple independent surveys concurrently.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_surveyor0_open_raw(&self._sock) if raw
                  else nng_surveyor0_open(&self._sock))

    @property
    def survey_time(self) -> int:
        """Survey deadline in milliseconds.

        After sending a survey, the socket accepts responses for this long.
        Once the deadline expires, :meth:`recv` raises :exc:`NngError` with
        ``NNG_ETIMEDOUT``.  The next :meth:`send` starts a fresh survey.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._sock, b"surveyor:survey-time", &v))
        return v

    @survey_time.setter
    def survey_time(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._sock, b"surveyor:survey-time", ms))

    def open_context(self) -> Context:
        """Open an independent surveyor context on this socket."""
        self._check()
        return Context.create(self)


cdef class RespondentSocket(Socket):
    """Respondent socket — receives surveys and sends back a single response each.

    The protocol is similar to REP: :meth:`recv` waits for an incoming survey,
    and :meth:`send` delivers the response back to the surveyor that sent it.
    Multiple respondents can reply to the same survey; the surveyor collects
    all replies within its deadline.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_respondent0_open_raw(&self._sock) if raw
                  else nng_respondent0_open(&self._sock))

    def open_context(self) -> Context:
        """Open an independent respondent context on this socket."""
        self._check()
        return Context.create(self)


cdef class BusSocket(Socket):
    """Bus socket — broadcasts each message to **all** other connected bus nodes.

    Every message sent on a ``BusSocket`` is forwarded to every other
    ``BusSocket`` peer connected to it.  Unlike PUB/SUB there is no topic
    filtering and the bus is symmetric: all peers can both send and receive.

    Typical use case: all-to-all communication where every node needs to hear
    about events from every other node.

    .. note::
       BUS does not provide delivery guarantees.  A slow peer whose send buffer
       is full will have messages dropped silently, without affecting other
       peers.
    """

    def __cinit__(self, *, bint raw=False):
        check_err(nng_bus0_open_raw(&self._sock) if raw
                  else nng_bus0_open(&self._sock))

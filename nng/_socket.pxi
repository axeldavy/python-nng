# nng/_socket.pxi – included into _nng.pyx
#
# PipeStatus    – IntEnum for pipe lifecycle
# Pipe          – non-owning wrapper for a pipe connection
# Socket        – base class, owns nng_socket
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
import enum as _enum

# ── Pipe event trampoline (GIL-free) ─────────────────────────────────────────

cdef void _pipe_trampoline(nng_pipe pipe,
                           nng_pipe_ev ev,
                           void *arg) noexcept nogil:
    """Push pipe event into the GIL-free queue; send sentinel to wake dispatcher.

    The socket id is captured here, while the pipe handle is still valid.
    REM_POST events arrive after nng has already destroyed the pipe handle,
    so calling nng_pipe_socket() at dispatch time would return an invalid id.
    """
    cdef uint32_t sock_id = nng_socket_id(nng_pipe_socket(pipe))
    _pipe_event_queue.push(sock_id, pipe.id, <int>ev)
    _dispatch_queue.push(0)   # sentinel – wakes dispatcher; id=0 is ignored


# ── PipeStatus ────────────────────────────────────────────────────────────────

class PipeStatus(_enum.IntEnum):
    """Lifecycle state of a :class:`Pipe`.

    * ``ADDING``  – the pipe has been created and negotiated (ADD_PRE event).
      The :meth:`Socket.on_new_pipe` callback fires at this point.
    * ``ACTIVE``  – the pipe is fully active (ADD_POST event).
      The pipe's own ``on_status_change`` callback fires here.
    * ``REMOVED`` – the pipe has been closed (REM_POST event).
      The pipe's ``on_status_change`` callback fires, then the pipe is
      removed from :attr:`Socket.pipes`.
    """
    ADDING  = NNG_PIPE_EV_ADD_PRE
    ACTIVE  = NNG_PIPE_EV_ADD_POST
    REMOVED = NNG_PIPE_EV_REM_POST


# ── Pipe ──────────────────────────────────────────────────────────────────────

cdef class Pipe:
    """Non-owning wrapper for a single nng pipe (physical connection).

    Pipes are created automatically:

    * A :class:`Dialer` creates one pipe when its outbound connection succeeds
      and recreates it transparently after each reconnect.
    * A :class:`Listener` creates one pipe for every inbound connection it
      accepts.

    Applications normally never create ``Pipe`` objects directly.  They appear
    as arguments to callbacks registered with :meth:`Socket.on_new_pipe`.
    The ``Pipe`` object is **non-owning** — closing it kicks the connection;
    the underlying socket is unaffected.
    """

    cdef PipeHandle _handle

    cdef object __weakref__

    @staticmethod
    cdef Pipe _from_nng(nng_pipe p):
        cdef Pipe obj = Pipe.__new__(Pipe)
        obj._handle = PipeHandle(p)
        return obj

    @property
    def id(self) -> int:
        """Numeric pipe ID."""
        return self._handle.get_id()

    def __index__(self): return self._handle.get_id()
    def __hash__(self):  return self._handle.get_id()
    def __eq__(self, other):
        if not isinstance(other, Pipe):
            return NotImplemented
        return self._handle == (<Pipe>other)._handle
    def __ne__(self, other):
        if not isinstance(other, Pipe):
            return NotImplemented
        return self._handle != (<Pipe>other)._handle

    @property
    def status(self) -> PipeStatus:
        """Current :class:`PipeStatus` of this pipe."""
        sock = _socket_registry.get(nng_socket_id(self._handle.get_socket()))
        if sock is None:
            return PipeStatus.REMOVED
        return (<Socket>sock).get_pipe_status(self._handle.get_id())

    @property
    def on_status_change(self):
        """Callback fired when this pipe transitions to ACTIVE or REMOVED."""
        sock = _socket_registry.get(nng_socket_id(self._handle.get_socket()))
        if sock is None:
            return None
        return (<Socket>sock).get_pipe_cb(self._handle.get_id())

    @on_status_change.setter
    def on_status_change(self, cb):
        sock = _socket_registry.get(nng_socket_id(self._handle.get_socket()))
        if sock is not None:
            (<Socket>sock).set_pipe_cb(self._handle.get_id(), cb)

    @property
    def socket(self):
        """The owning :class:`Socket`, or ``None`` if no longer tracked."""
        return _socket_registry.get(nng_socket_id(self._handle.get_socket()))

    @property
    def dialer(self):
        """The :class:`Dialer` that created this pipe, or ``None``."""
        cdef nng_dialer d = self._handle.get_dialer()
        if d.id == 0:
            return None
        # Locate within the owning socket's dialer list
        sock = _socket_registry.get(nng_socket_id(self._handle.get_socket()))
        if sock is None:
            return None
        for dl in (<Socket>sock)._dialers:
            if (<Dialer>dl)._handle.id() == d.id:
                return dl
        return None

    @property
    def listener(self):
        """The :class:`Listener` that accepted this pipe, or ``None``."""
        cdef nng_listener li = self._handle.get_listener()
        if li.id == 0:
            return None
        sock = _socket_registry.get(nng_socket_id(self._handle.get_socket()))
        if sock is None:
            return None
        for ln in (<Socket>sock)._listeners:
            if (<Listener>ln)._handle.id() == li.id:
                return ln
        return None

    def close(self) -> None:
        """Forcibly close this connection.

        The underlying transport is terminated immediately.  Dialers attempt to
        reconnect; listeners wait for a new inbound connection.  In-flight
        sends/receives on this pipe fail immediately.
        """
        check_err(self._handle.close())

    def __repr__(self) -> str:
        return f"Pipe(id={self._handle.get_id()}, status={self.status.name})"


# ── Pipe-event constants (re-exported at module level for backward compat) ────
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
    * :class:`PairSocket` — one active pipe at a time
    * :class:`BusSocket` — send to *all other* peers.
    * :class:`SurveyorSocket` — broadcast survey; collect all responses.

    ** Thread-safety and concurrency **
    All socket methods are thread-safe and can be called concurrently from
    multiple threads.
    `submit_send`, `submit_recv`, `asend`, and `arecv` internally submit an
    asynchronous operation to the socket's I/O thread and return a Future,
    avoiding to block until completion. They can be called concurrently
    with each other and with `recv`/`send`, as well as from different
    threads or asyncio loops.

    **Available transports**

    The URL scheme passed to :meth:`add_dialer` / :meth:`add_listener` selects
    the transport:

    * **TCP** – ``tcp://<host>:<port>``

      Cross-machine communication over TCP/IP.  Use ``tcp4://`` or ``tcp6://``
      to force IPv4 or IPv6.  When listening, omit the host or use ``0.0.0.0``
      / ``::`` to accept on all interfaces, e.g. ``tcp://0.0.0.0:5555`` or
      simply ``tcp://:5555``.  IPv6 literals must be bracketed:
      ``tcp://[::1]:5555``.

    * **IPC** – ``ipc://<path>``

      Inter-process communication on the same host.  Uses UNIX domain sockets
      on POSIX and named pipes on Windows.  On POSIX, prefer absolute paths to
      avoid working-directory sensitivity.  ``unix://`` is an alias for
      ``ipc://`` on POSIX systems.

    * **Abstract** (Linux only) – ``abstract://<name>``

      Linux abstract-namespace sockets.  Names are URI-encoded and do not
      appear in the filesystem; they are freed automatically when no longer in
      use.  Embedded NUL bytes can be encoded as ``%00``, e.g.
      ``abstract://foo%00bar``.

    * **In-process** – ``inproc://<name>``

      Zero-copy communication between sockets **within the same process**.
      The name is an arbitrary string; different names are completely
      independent. Sockets in separate processes, or using different NNG
      library instances, cannot communicate via inproc.

    * **UDP** *(experimental)* – ``udp://<host>:<port>``

      Unreliable, connectionless transport.  Messages are ordered by nng but
      gaps are possible; there is no delivery guarantee.  Maximum message size
      is effectively ~1140 bytes on standard Ethernet (65 000 B hard cap).
      Use ``udp4://`` or ``udp6://`` to restrict to a specific IP version.
      IPv6 literals must be bracketed: ``udp://[::1]:5555``.

    **Closing**

    Closing a socket (via :meth:`close` or exiting a ``with`` block) waits for
    outstanding operations to finish or be aborted.  All associated dialers,
    listeners, pipes, and contexts are invalidated.

    **Typical usage**::

        with ReqSocket() as sock:
            sock.add_dialer("tcp://127.0.0.1:5555").start()
            sock.send(b"ping")
            reply = sock.recv()
    """

    cdef shared_ptr[SocketHandle] _handle
    # Strong references to all dialers/listeners created on this socket so
    # they are not garbage-collected while the socket is open.
    cdef list _dialers
    cdef list _listeners

    # ── Pipe tracking (guarded by _pipe_mutex) ────────────────────────────
    cdef DCGMutex _pipe_mutex
    cdef list     _pipes            # ordered list of live Pipe objects
    cdef dict     _pipe_statuses    # int(pipe_id) → PipeStatus
    cdef dict     _pipe_cbs_table   # int(pipe_id) → callable | None
    cdef object   _on_new_pipe_cb   # callable(Pipe) | None (ADD_PRE)

    # For polling. Lazy init.
    cdef int _fd_recv
    cdef int _fd_send

    cdef object __weakref__

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.
        self._dialers = []
        self._listeners = []
        self._pipes = []
        self._pipe_statuses = {}
        self._pipe_cbs_table = {}
        self._on_new_pipe_cb = None
        self._fd_recv = -1
        self._fd_send = -1

    cdef void _setup_pipe_tracking(self) except *:
        """Register the GIL-free trampoline and add socket to the registry.

        Must be called from each concrete subclass __cinit__ immediately after
        self._handle is assigned (i.e. after nng_xxx_open succeeds).
        """
        cdef nng_socket raw = self._handle.get().raw()
        check_err(nng_pipe_notify(raw, NNG_PIPE_EV_ADD_PRE,  _pipe_trampoline, NULL))
        check_err(nng_pipe_notify(raw, NNG_PIPE_EV_ADD_POST, _pipe_trampoline, NULL))
        check_err(nng_pipe_notify(raw, NNG_PIPE_EV_REM_POST, _pipe_trampoline, NULL))
        _socket_registry[nng_socket_id(raw)] = self

    # __dealloc__ intentionally omitted: shared_ptr[SocketHandle] destructor
    # calls SocketHandle::~SocketHandle() -> nng_socket_close() when ref-count hits 0.

    # ── Pipe management cdef methods ──────────────────────────────────────────

    cdef void add_pipe(self, nng_pipe p):
        """Create a Pipe for *p*, record ADDING status, fire on_new_pipe callback.

        Called by the dispatch thread at ADD_PRE.  The _on_new_pipe_cb is
        copied out under the mutex then called without the mutex held to prevent
        deadlock if the callback re-enters socket methods.
        """
        cdef Pipe pipe = Pipe._from_nng(p)
        cdef int pid = pipe.id
        cdef object cb
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        self._pipes.append(pipe)
        self._pipe_statuses[pid] = PipeStatus.ADDING
        cb = self._on_new_pipe_cb
        lock.unlock()
        if cb is not None:
            try:
                cb(pipe)
            except BaseException:
                _print_exc()

    cdef void promote_pipe(self, uint32_t pid):
        """Transition pipe *pid* from ADDING → ACTIVE and fire its callback."""
        cdef object cb
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        self._pipe_statuses[<int>pid] = PipeStatus.ACTIVE
        cb = self._pipe_cbs_table.get(<int>pid)
        lock.unlock()
        if cb is not None:
            try:
                cb()
            except BaseException:
                _print_exc()

    cdef void remove_pipe(self, uint32_t pid):
        """Set pipe *pid* to REMOVED, fire callback, then remove from tables.

        The callback fires while the pipe still appears in *_pipes* (with
        REMOVED status) so the callback can inspect *socket.pipes*.
        After the callback returns the pipe is removed from all tracking tables.
        """
        cdef object cb
        cdef int i
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        self._pipe_statuses[<int>pid] = PipeStatus.REMOVED
        cb = self._pipe_cbs_table.get(<int>pid)
        lock.unlock()
        if cb is not None:
            try:
                cb()
            except BaseException:
                _print_exc()
        # Remove from tracking tables after callback has returned.
        lock_gil_friendly(lock, self._pipe_mutex)
        for i in range(len(self._pipes)):
            if (<Pipe>self._pipes[i])._handle.get_id() == <int>pid:
                del self._pipes[i]
                break
        self._pipe_statuses.pop(<int>pid, None)
        self._pipe_cbs_table.pop(<int>pid, None)

    cdef object get_pipe_status(self, int pid):
        """Return the current :class:`PipeStatus` for *pid* (REMOVED if unknown)."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        return self._pipe_statuses.get(pid, PipeStatus.REMOVED)

    cdef void set_pipe_cb(self, int pid, object cb):
        """Register or clear the on_status_change callback for *pid*."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        if cb is None:
            self._pipe_cbs_table.pop(pid, None)
        else:
            self._pipe_cbs_table[pid] = cb

    cdef object get_pipe_cb(self, int pid):
        """Return the on_status_change callback for *pid*, or ``None``."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        return self._pipe_cbs_table.get(pid)

    cdef inline void _check(self) except *:
        if not self._handle or not self._handle.get().is_open():
            raise NngClosed(NNG_ECLOSED, "Socket is closed")

    def close(self) -> None:
        """Close the socket and release all resources.

        Waits for any outstanding operations to complete or be aborted before
        returning.  All associated :class:`Dialer`, :class:`Listener`,
        :class:`Pipe`, and :class:`Context` objects are invalidated.

        .. note::
            This method blocks until in-flight operations finish.  Do not call
            it from a context that cannot block.  Closing the socket may
            disrupt transfers that are still in progress.
        """
        cdef unique_lock[DCGMutex] lock
        if self._handle and self._handle.get().is_open():
            # Clear pipe tracking before closing so callbacks cannot fire after close.
            lock_gil_friendly(lock, self._pipe_mutex)
            self._on_new_pipe_cb = None
            self._pipes.clear()
            self._pipe_statuses.clear()
            self._pipe_cbs_table.clear()
            lock.unlock()
            with nogil:
                self._handle.get().close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def id(self) -> int:
        """The underlying nng socket ID (positive) or 0 if closed."""
        if not self._handle or not self._handle.get().is_open():
            return 0
        return nng_socket_id(self._handle.get().raw())

    @property
    def proto_name(self) -> str:
        """Protocol name (e.g. ``"pair1"``)."""
        self._check()
        cdef const char *name = NULL
        check_err(nng_socket_proto_name(self._handle.get().raw(), &name))
        return name.decode("utf-8")

    @property
    def peer_name(self) -> str:
        """Peer protocol name."""
        self._check()
        cdef const char *name = NULL
        check_err(nng_socket_peer_name(self._handle.get().raw(), &name))
        return name.decode("utf-8")

    # ── Options ───────────────────────────────────────────────────────────

    @property
    def recv_timeout(self) -> int:
        """Receive timeout in ms (-1 = infinite, 0 = non-blocking)."""
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), b"recv-timeout", &v))
        return v

    @recv_timeout.setter
    def recv_timeout(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), b"recv-timeout", ms))

    @property
    def send_timeout(self) -> int:
        """Send timeout in ms."""
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), b"send-timeout", &v))
        return v

    @send_timeout.setter
    def send_timeout(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), b"send-timeout", ms))

    @property
    def recv_buf(self) -> int:
        """Receive buffer depth (number of queued messages)."""
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), b"recv-buffer", &v))
        return v

    @recv_buf.setter
    def recv_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), b"recv-buffer", n))

    @property
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages)."""
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), b"send-buffer", &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), b"send-buffer", n))

    @property
    def recv_max_size(self) -> int:
        """Maximum incoming message size in bytes (0 = no limit)."""
        self._check()
        cdef size_t v
        check_err(nng_socket_get_size(self._handle.get().raw(), b"recv-size-max", &v))
        return v

    @recv_max_size.setter
    def recv_max_size(self, size_t n):
        self._check()
        check_err(nng_socket_set_size(self._handle.get().raw(), b"recv-size-max", n))

    @property
    def recv_fd(self) -> int:
        """Readable file descriptor for external poll / epoll / kqueue.

        Becomes readable when the socket has data to receive.
        Never read or write to this fd directly.
        """
        self._check()
        cdef int fd
        if self._fd_recv == -1:
            check_err(nng_socket_get_recv_poll_fd(self._handle.get().raw(), &fd))
            self._fd_recv = fd

        if self._fd_recv == -1:
            raise NngError(NNG_EINTERNAL, "Socket does not support recv polling")

        return self._fd_recv

    @property
    def send_fd(self) -> int:
        """Writable file descriptor for external poll / epoll / kqueue.

        Becomes writable when the socket can accept a send.
        """
        self._check()
        cdef int fd
        if self._fd_send == -1:
            check_err(nng_socket_get_send_poll_fd(self._handle.get().raw(), &fd))
            self._fd_send = fd

        if self._fd_send == -1:
            raise NngError(NNG_EINTERNAL, "Socket does not support send polling")

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

        Raises
        ------
        NngClosed
            If the socket has been closed.
        NngNotSupported
            If *url* uses a transport scheme that is not recognized by nng
            (e.g. ``"unknown://host:1234"``).  Note: nng raises NngNotSupported
            rather than NngInvalidArgument for unrecognized transport names.
        NngInvalidArgument
            If *url* is otherwise syntactically invalid for a known transport.
        """
        self._check()
        cdef bytes b = _encode_url(url)
        cdef int err = 0
        cdef DialerHandle dh = make_dialer(self._handle, b, err)
        check_err(err)
        # Options: if any check_err raises, dh's destructor auto-calls nng_dialer_close.
        if tls is not None:
            check_err(dh.set_tls((<TlsConfig?>tls)._get_ptr()))
        if reconnect_min_ms is not None:
            check_err(dh.set_ms(b"reconnect-time-min", reconnect_min_ms))
        if reconnect_max_ms is not None:
            check_err(dh.set_ms(b"reconnect-time-max", reconnect_max_ms))
        if recv_timeout is not None:
            check_err(dh.set_ms(b"recv-timeout", recv_timeout))
        if send_timeout is not None:
            check_err(dh.set_ms(b"send-timeout", send_timeout))
        cdef Dialer obj = Dialer.create(move(dh))
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

        Raises
        ------
        NngClosed
            If the socket has been closed.
        NngNotSupported
            If *url* uses a transport scheme that is not recognized by nng
            (e.g. ``"unknown://host:1234"``).  Note: nng raises NngNotSupported
            rather than NngInvalidArgument for unrecognized transport names.
        NngInvalidArgument
            If *url* is otherwise syntactically invalid for a known transport.
        NngAddressInUse
            If the address is already bound by another listener.
        """
        self._check()
        cdef bytes b = _encode_url(url)
        cdef int err = 0
        cdef ListenerHandle lh = make_listener(self._handle, b, err)
        check_err(err)
        # Options: if any check_err raises, lh's destructor auto-calls nng_listener_close.
        if tls is not None:
            check_err(lh.set_tls((<TlsConfig?>tls)._get_ptr()))
        if recv_timeout is not None:
            check_err(lh.set_ms(b"recv-timeout", recv_timeout))
        if send_timeout is not None:
            check_err(lh.set_ms(b"send-timeout", send_timeout))
        cdef Listener obj = Listener.create(move(lh))
        self._listeners.append(obj)
        return obj

    # ── Synchronous send / recv ───────────────────────────────────────────

    def send(self, data, *, bint nonblock=False) -> None:
        """Send an :class:`Message`.  Transfers ownership on success.
        
        Parameters
        ----------
        msg:
            The :class:`Message` to send.
        nonblock:
            If *True* raise :exc:`NngAgain` instead of blocking.

        NOTE: Signals such as KeyBoardInterrupt (Ctrl-C) are not handled
        during this function. Thus avoid blocking calls on the main thread.
        Use submit_send().result() for a blocking call that handles Ctrl-C.
        """
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
            rv = nng_sendmsg(self._handle.get().raw(), ptr, flags)

        # Raise on errors and restore/free message
        if rv != 0:
            if message_is_copy:
                # nng did not take ownership; free the internally created copy.
                nng_msg_free(ptr)
            else:
                # nng did not take ownership; restore it to the Python wrapper.
                (<Message>data)._handle.restore(ptr)
            check_err(rv)

    def recv(self, *, bint nonblock=False) -> Message:
        """Receive and return an :class:`Message` (zero-copy via buffer protocol).
        
        Parameters
        ----------
        nonblock:
            If *True* raise :exc:`NngAgain` if no message is ready.

        NOTE: Signals such as KeyBoardInterrupt (Ctrl-C) are not handled
        during this function. Thus avoid blocking calls on the main thread.
        Use submit_recv().result() for a blocking call that handles Ctrl-C.
        """
        self._check()

        # Receive incoming message
        cdef nng_msg *ptr = NULL
        cdef int flags = NNG_FLAG_NONBLOCK if nonblock else 0
        cdef int rv
        with nogil:
            rv = nng_recvmsg(self._handle.get().raw(), &ptr, flags)

        # Raise on error
        check_err(rv)

        # Return message
        return Message._from_ptr(ptr)

    # ── Async send / recv ─────────────────────────────────────────────────

    async def asend(self, data) -> None:
        """Send bytes asynchronously (asyncio-compatible).
        
        NOTE: if the coroutine is cancelled while waiting for send
        completion, the data may or may not have been sent.
        """
        self._check()

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
            rv = nng_sendmsg(self._handle.get().raw(), ptr, NNG_FLAG_NONBLOCK)

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

        # Create an aio request
        cdef _AioOp op = _AioOp.create_for_socket(self._handle)

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
        op.submit_socket_send(self._handle.get().raw())

        # Await completion
        try:
            await future
        except _asyncio.CancelledError:
            op.on_future_cancel()
            raise
        finally:
            op.ensure_finish()
            

    async def arecv(self):
        """Receive bytes asynchronously (asyncio-compatible)."""
        cdef size_t result_len
        cdef bytes result

        self._check()

        # Fast path: non-blocking recv can perform in the same
        # thread, avoiding thread dispatch.
        cdef nng_msg *msg = NULL
        cdef int rv
        with nogil:
            rv = nng_recvmsg(self._handle.get().raw(), &msg, NNG_FLAG_NONBLOCK)

        # On success the message is received and we can return it.
        if rv == NNG_OK:
            return Message._from_ptr(msg)

        # On EAGAIN the message was not received but we can still proceed to the slow path.
        # On any other error the message was not received and we must raise immediately.
        if rv != NNG_EAGAIN:
            check_err(rv)

        # Slow path: recv queue was empty; use the async aio machinery.
        cdef _AioOp op = _AioOp.create_for_socket(self._handle)

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        op.submit_socket_recv(self._handle.get().raw())

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
            raise NngError(NNG_EINTERNAL, "No message in AIO after async recv")

        # Take ownership of the message and return it
        return Message._from_ptr(ptr)

    # ── concurrent.futures submit helpers ─────────────────────────────────

    def submit_send(self, data) -> _concurrent_futures.Future:
        """Send *data* (bytes-like) asynchronously; return a concurrent.futures.Future.

        The future resolves to ``None`` on success or raises an :exc:`NngError`
        on failure. Can be called from any thread.
        """
        self._check()

        # Create an aio request
        cdef _AioOp op = _AioOp.create_for_socket_concurrent(self._handle, False)

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
        op.submit_socket_send(self._handle.get().raw())

        # Return the concurrent.futures.Future to the caller
        return fut

    def submit_recv(self) -> _concurrent_futures.Future:
        """Receive asynchronously; return a concurrent.futures.Future → bytes.

        The future resolves to the received payload as ``bytes``.
        Can be called from any thread.
        """
        self._check()

        # Create an aio request
        cdef _AioOp op = _AioOp.create_for_socket_concurrent(self._handle, True)

        # Fetch the future now as get_future is invalid after submission
        fut = op.get_future()

        # Submit the operation
        op.submit_socket_recv(self._handle.get().raw())

        # Return the concurrent.futures.Future to the caller
        return fut

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
            await watcher.aclose()
        """
        self._check()

        # Initialize fd_recv
        cdef int fd
        if self._fd_recv == -1:
            check_err(nng_socket_get_recv_poll_fd(self._handle.get().raw(), &fd))
            self._fd_recv = fd
        else:
            fd = self._fd_recv

        if fd == -1:
            raise NngError(NNG_EINTERNAL, "Socket does not support recv polling")

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
            await send_watcher.aclose()
        """
        self._check()

        # Initialize fd_send
        cdef int fd
        if self._fd_send == -1:
            check_err(nng_socket_get_send_poll_fd(self._handle.get().raw(), &fd))
            self._fd_send = fd
        else:
            fd = self._fd_send

        if fd == -1:
            raise NngError(NNG_EINTERNAL, "Socket does not support send polling")

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

    # ── Pipe access ───────────────────────────────────────────────────────────

    @property
    def pipes(self) -> list:
        """Snapshot list of currently live :class:`Pipe` objects.

        The list is a copy; it will not change as connections are added or
        removed.  Inspect :attr:`Pipe.status` for the state of each pipe.
        """
        self._check()
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        return list(self._pipes)

    def on_new_pipe(self, callback) -> None:
        """Register a callback invoked when a new connection is negotiated.

        The callback is called at :attr:`PipeStatus.ADDING` – before the pipe
        is admitted to the socket pool.  Calling :meth:`Pipe.close` inside
        the callback rejects the connection entirely.

        Only one callback is kept; calling this method again replaces the
        previous one.  Pass ``None`` to disable.

        Parameters
        ----------
        callback:
            ``(pipe: Pipe) -> None``.  Executed on the dispatcher thread;
            keep it short and non-blocking.
        """
        self._check()
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        self._on_new_pipe_cb = callback

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self._handle.get().raw().id if self._handle else 0})"


# ── Helper ────────────────────────────────────────────────────────────────────

cdef inline bytes _encode_url(url):
    if isinstance(url, bytes):
        return <bytes> url
    return (<str> url).encode("utf-8")


# ── Protocol subclasses ───────────────────────────────────────────────────────

# We do not support raw sockets as it involves header editing and many potential
# pitfalls, at which point it is better that advanced users use the C API directly.

cdef class PairSocket(Socket):
    """Bidirectional, point-to-point socket (Pair protocol).

    A pair socket can send and receive freely in both directions.  In standard
    mode it is strictly **one-to-one**: only a single pipe is active at a time.
    Messages sent while no pipe is connected are held in the send buffer.

    .. note::
       PAIR does **not** guarantee reliable delivery.  Although back-pressure
       prevents most discards, some topologies can still lose messages.
       Applications that need reliable delivery should use :class:`ReqSocket`
       or add their own acknowledgment layer.

    **Protocol versions**

    The default is Pair v1.  Pass ``v0=True`` for the legacy v0 protocol, which
    lacks hop-count headers and is required when interoperating with older
    implementations such as *libnanomsg* or *mangos*.

    Parameters
    ----------
    v0:
        Use the older Pair v0 protocol instead of the default v1.
    """

    def __cinit__(self, *, bint v0=False):
        cdef nng_socket raw_sock
        if v0:
            check_err(nng_pair0_open(&raw_sock))
        else:
            check_err(nng_pair1_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

# TODO: v1 has NNG_OPT_MAXTTL

cdef class PubSocket(Socket):
    """Publish socket — broadcasts every message to **all** connected subscribers.

    A message sent on a ``PubSocket`` is delivered to every :class:`SubSocket`
    peer that has a matching subscription prefix.  Peers with no matching
    subscription silently discard the message.

    * **Fan-out**: one send → copied to all active pipes.
    * **Topic filtering**: filtering happens on the *subscriber* side based on the
      message body prefix (see :meth:`SubSocket.subscribe`).  The publisher
      always sends every message to every subscriber; subscriptions do **not**
      reduce network bandwidth.
    * **No back-pressure**: a slow subscriber does not block the publisher;
      messages for that subscriber are dropped when its send buffer is full.
    * PubSocket cannot receive; :meth:`recv` raises ``NngNotSupported``.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_pub0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()


cdef class SubSocket(Socket):
    """Subscribe socket — receives messages from a :class:`PubSocket`.

    By default a ``SubSocket`` receives **nothing**.  You must subscribe to at
    least one topic prefix with :meth:`subscribe` before any messages arrive.
    The socket can hold multiple subscriptions simultaneously.

    Messages whose body does *not* start with at least one registered prefix
    are silently dropped by nng before they reach the application.  Topic
    filtering is done **locally** on the subscriber side — the publisher always
    sends every message to every subscriber, so subscriptions do not reduce
    bandwidth consumption.

    * SubSocket is receive-only; :meth:`send` raises ``NngNotSupported``.
    * One ``SubSocket`` can connect to multiple publishers by calling
      :meth:`add_dialer` several times.
    * When the receive queue is full, **newer** messages are kept and older
      unread messages are discarded by default.

    Example::

        sub = SubSocket()
        sub.add_dialer("tcp://127.0.0.1:5556").start()
        sub.subscribe(b"")       # receive every message
        sub.subscribe(b"news.")  # also accept prefix-matched topics
        msg = sub.recv()
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_sub0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

    def subscribe(self, prefix: bytes = b"") -> None:
        """Subscribe to messages whose body starts with *prefix*.

        An empty prefix subscribes to all messages.
        """
        self._check()
        cdef const unsigned char[::1] mv = prefix
        check_err(nng_sub0_socket_subscribe(
            self._handle.get().raw(), &mv[0] if len(mv) else NULL, len(mv)))

    def unsubscribe(self, prefix: bytes = b"") -> None:
        """Remove a topic subscription."""
        self._check()
        cdef const unsigned char[::1] mv = prefix
        check_err(nng_sub0_socket_unsubscribe(
            self._handle.get().raw(), &mv[0] if len(mv) else NULL, len(mv)))

    @property
    def skip_older_on_full_queue(self) -> bool:
        """Whether to drop older messages when the receive queue is full.

        By default, when the receive queue is full, newer messages are kept and
        older unread messages are dropped.  Setting this option to ``False``
        reverses that behavior: older messages are kept and newer messages are
        dropped instead.
        """
        self._check()
        cdef cpp_bool v
        check_err(nng_socket_get_bool(self._handle.get().raw(), b"sub:prefnew", &v))
        return bool(v)

    @skip_older_on_full_queue.setter
    def skip_older_on_full_queue(self, bint skip_older):
        self._check()
        check_err(nng_socket_set_bool(self._handle.get().raw(), b"sub:prefnew", skip_older))

    def open_context(self) -> SubContext:
        """Open an independent sub context on this socket."""
        self._check()
        return SubContext.open(self)


cdef class ReqSocket(Socket):
    """Request socket — sends a request and waits for exactly one reply.

    The REQ/REP protocol enforces a strict send → receive → send → … cycle on
    each context.  Violating this order (e.g. two sends in a row) raises an
    error.

    **Message routing with multiple peers**

    When the socket has several pipes (e.g. after multiple :meth:`add_dialer`
    calls or when connected to a server with multiple workers via contexts),
    each request is sent to the **first available** pipe — whichever peer is
    ready to accept it.  The reply is expected to arrive on the *same* pipe.

    If the pipe dies before the reply arrives, the request is automatically
    resent on another available pipe (see :attr:`resend_time`).  Because
    requests may be retransmitted, they should be **idempotent** — executing
    the same request twice must produce the same result.  If that is not
    possible, set :attr:`resend_time` to ``-1`` to disable automatic resending.

    Cancelling a pending request is done by sending a new request; any reply
    from the cancelled request that arrives later is silently discarded.

    **Concurrent requests**

    A bare ``ReqSocket`` allows only one in-flight request at a time.  To
    issue multiple concurrent requests, open independent contexts with
    :meth:`open_context`.  Each context runs its own REQ state machine and can
    be in-flight on a different pipe simultaneously.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_req0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

    @property
    def resend_time(self) -> int:
        """How long (ms) to wait for a reply before resending the request.

        Requests are automatically resent if the peer disconnects.

        resend_time adds a timer at which requests will be resent (even if no
        disconnection has been detected) to the peer until a reply is received.
    
        ``-1`` (the default) disables automatic resending (exception on disconnect)
        Setting a positive value provides automatic fault-tolerance when working with
        multiple REP peers, or possible packet loss.

        Setting a < 1000ms resend_time requires tuning :attr:`resend_tick` to
        have real effect.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), b"req:resend-time", &v))
        return v

    @resend_time.setter
    def resend_time(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), b"req:resend-time", ms))

    @property
    def resend_tick(self) -> int:
        """How often (ms) to check for timed-out requests to resend.

        The socket checks for timed-out requests at this interval.
        Lower values add overhead, but are needed for short resend_time values.
        
        The default is 1000 ms.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), b"req:resend-tick", &v))
        return v

    @resend_tick.setter
    def resend_tick(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), b"req:resend-tick", ms))

    def open_context(self) -> Context:
        """Open an independent REQ context on this socket.

        Each context has its own send → receive → send state machine and can
        have a request in-flight on a different pipe at the same time as other
        contexts.  This is the preferred way to pipeline concurrent requests
        without opening multiple sockets.
        """
        self._check()
        return Context.open(self)


cdef class RepSocket(Socket):
    """Reply socket — receives a request and sends exactly one reply.

    The REP/REQ protocol enforces a strict receive → send → receive → … cycle
    on each context.  Attempting to send a reply before receiving a request, or
    to receive a second request before replying to the first, raises
    ``NngState``.

    **Serving multiple concurrent clients**

    A single ``RepSocket`` can be connected to any number of request senders
    (via :meth:`add_listener` or :meth:`add_dialer`).  However, the
    socket-level :meth:`recv` / :meth:`send` cycle handles only **one request
    at a time** — only one receive operation may be pending simultaneously.
    To serve multiple clients concurrently without blocking, open independent
    per-client contexts with :meth:`open_context`; each context carries the
    reply-routing state for one in-flight request.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_rep0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

    def open_context(self) -> Context:
        """Open an independent REP context on this socket.

        Each context can receive one request and send one reply independently
        of all other contexts.  Use this to serve multiple clients concurrently
        from a single socket without a thread per client.
        """
        self._check()
        return Context.open(self)


cdef class PushSocket(Socket):
    """Push socket — distributes messages across connected pull peers (pipeline).

    PUSH/PULL implements a fan-out work queue.  Each message sent on a
    ``PushSocket`` is delivered to **exactly one** :class:`PullSocket` peer,
    chosen by a fair round-robin / load-balancing algorithm that favours peers
    with available buffer space.

    * Messages are never duplicated — each goes to one worker.
    * If there are no connected peers the send blocks (or raises
      :exc:`NngAgain` in non-blocking mode) until a peer is available.  Back-
      pressure is honoured: only peers capable of accepting a message are
      considered.
    * PushSocket is send-only; :meth:`recv` raises ``NngNotSupported``.
    * **No delivery guarantee**: there is no acknowledgment mechanism.  If
      reliable delivery is required, consider :class:`ReqSocket` instead.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_push0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

    @property
    def send_buffer_length(self) -> int:
        """Maximum number of messages that can be buffered for sending to a peer.

        Default (0), means operations are unbuffers, and sending will block
        until a peer is available to receive the message.

        Setting a positive value queues to an intermediate buffer of that many
        messages.

        The maximum value is 8192.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), b"send-buffer", &v))
        return v

    @send_buffer_length.setter
    def send_buffer_length(self, int length):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), b"send-buffer", length))

cdef class PullSocket(Socket):
    """Pull socket — receives work items distributed by a :class:`PushSocket`.

    Each message arrives from exactly one push sender and is delivered to
    exactly this socket (not duplicated to others).  Multiple ``PullSocket``
    instances connected to the same ``PushSocket`` act as a pool of workers:
    nng load-balances across them automatically.

    When two peers both have a message ready simultaneously, the order in which
    messages are delivered to the application is undefined.

    * PullSocket is receive-only; :meth:`send` raises ``NngNotSupported``.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_pull0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()


cdef class SurveyorSocket(Socket):
    """Surveyor socket — broadcasts a survey and collects responses within a deadline.

    Sending a message broadcasts it to **all** connected
    :class:`RespondentSocket` peers.  Respondents are not obliged to reply;
    the surveyor collects as many responses as arrive within :attr:`survey_time`
    milliseconds.  After the deadline expires, further :meth:`recv` calls raise
    :exc:`NngError` with ``NngTimeout`` and :meth:`recv` before any survey
    has been sent raises ``NngState``.

    Sending a new survey **cancels** the previous one; any responses from the
    earlier survey that arrive afterwards are silently discarded.

    Each connected respondent normally sends at most one reply per survey,
    though duplicates are possible in some topologies.

    Useful for solving voting / leader-election and service-discovery problems.

    Each send opens a new survey; the previous survey's results are discarded.
    Use :meth:`open_context` to run multiple independent surveys concurrently;
    each context maintains its own deadline and does not cancel surveys on
    other contexts.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_surveyor0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

    @property
    def survey_time(self) -> int:
        """Survey deadline in milliseconds.

        After sending a survey, the socket accepts responses for this long.
        Once the deadline expires, :meth:`recv` raises :exc:`NngError` with
        ``NngTimeout``.  The next :meth:`send` starts a fresh survey.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), b"surveyor:survey-time", &v))
        return v

    @survey_time.setter
    def survey_time(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), b"surveyor:survey-time", ms))

    def open_context(self) -> Context:
        """Open an independent surveyor context on this socket."""
        self._check()
        return Context.open(self)


cdef class RespondentSocket(Socket):
    """Respondent socket — receives surveys and sends back a single response each.

    The protocol is similar to REP: :meth:`recv` waits for an incoming survey,
    and :meth:`send` delivers the response back to the surveyor that sent it.
    Multiple respondents can reply to the same survey; the surveyor collects
    all replies within its deadline.

    A respondent may choose to discard a survey by simply not replying to it.
    Attempting to :meth:`send` without first receiving a survey raises
    ``NngState``.

    Useful for voting / leader-election and service-discovery problems.

    Use :meth:`open_context` to receive and respond to multiple surveys in
    parallel; each context has its own independent state machine.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_respondent0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

    def open_context(self) -> Context:
        """Open an independent respondent context on this socket."""
        self._check()
        return Context.open(self)


cdef class BusSocket(Socket):
    """Bus socket — broadcasts each message to **all** other connected bus nodes.

    Every message sent on a ``BusSocket`` is forwarded to every other
    ``BusSocket`` peer **directly** connected to it.  Unlike PUB/SUB there is
    no topic filtering and the bus is symmetric: all peers can both send and
    receive.

    Typical use case: all-to-all communication where every node needs to hear
    about events from every other node.

    .. important::
       Messages are only delivered to **directly** connected peers.  Peers
       connected indirectly (e.g. via an intermediate node) will **not**
       receive them.  To achieve true all-to-all communication you must build
       a fully-connected mesh.

    .. note::
       BUS delivery is **best-effort**: sends never block.  If a peer cannot
       receive (e.g. its buffer is full), its copy of the message is silently
       discarded without affecting other peers.  The protocol is not suitable
       for high-throughput workloads where loss would be unacceptable.
    """

    def __cinit__(self):
        cdef nng_socket raw_sock
        check_err(nng_bus0_open(&raw_sock))
        self._handle = make_shared[SocketHandle](raw_sock)
        self._setup_pipe_tracking()

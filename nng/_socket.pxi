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

from asyncio import get_running_loop as _get_running_loop
from cpython.bytes cimport PyBytes_FromStringAndSize
from selectors import DefaultSelector as _DefaultSelector, EVENT_READ as _EVENT_READ
import enum as _enum
from os import getpid as _getpid
import socket as _pysocket

from libcpp.memory cimport shared_ptr

cdef nng_sockaddr _ip_str_to_sockaddr(str ip_str):
    """Convert a dotted-decimal or colon-hex IP address string to nng_sockaddr.

    The port in the returned struct is always zero — nng ignores the port when
    binding a source address for an outgoing TCP connection.

    Raises
    ------
    ValueError
        If *ip_str* is not a valid IPv4 or IPv6 address.
    """
    cdef nng_sockaddr sa
    memset(&sa, 0, sizeof(sa))

    # Try IPv4 first, then IPv6.
    try:
        packed = _pysocket.inet_pton(_pysocket.AF_INET, ip_str)
        sa.s_in.sa_family = NNG_AF_INET
        sa.s_in.sa_port   = 0
        memcpy(&sa.s_in.sa_addr, <const char*>packed, 4)
        return sa
    except OSError:
        pass

    try:
        packed = _pysocket.inet_pton(_pysocket.AF_INET6, ip_str)
        sa.s_in6.sa_family = NNG_AF_INET6
        sa.s_in6.sa_port   = 0
        sa.s_in6.sa_scope  = 0
        memcpy(sa.s_in6.sa_addr, <const char*>packed, 16)
        return sa
    except OSError:
        pass

    raise ValueError(f"Invalid IP address: {ip_str!r}")

# ─ Socket address ─────────────────────────────────────────────
cdef class SocketAddr:
    """Utility for converting nng_sockaddr to a URL string."""
    cdef nng_sockaddr _sa
    cdef int          _pid  # Cached peer PID if supported by the transport; -1 if unsupported.

    def __init__(self):
        raise TypeError("SocketAddr cannot be instantiated directly")

    @staticmethod
    cdef SocketAddr create(nng_sockaddr sa, int peer_pid):
        cdef SocketAddr obj = SocketAddr.__new__(SocketAddr)
        obj._sa = sa
        obj._pid = peer_pid  # Initialize with provided peer PID
        return obj

    @property
    def pid(self) -> int | None:
        """Peer PID, or None if not supported."""
        if self._pid == -1:
            return None
        return self._pid

    @property
    def port(self) -> int:
        """Port number, or 0 if not applicable."""
        return nng_sockaddr_port(&self._sa)

    def __str__(self):
        cdef char[NNG_MAXADDRSTRLEN+1] buffer
        memset(buffer, 0, NNG_MAXADDRSTRLEN+1)
        cdef bytes buf = nng_str_sockaddr(&self._sa, buffer, NNG_MAXADDRSTRLEN)
        return buf.decode("utf-8")

    def __hash__(self) -> int:
        return nng_sockaddr_hash(&self._sa)

    def __eq__(self, other) -> bool:
        if not isinstance(other, SocketAddr):
            return NotImplemented
        return nng_sockaddr_equal(&self._sa, &(<SocketAddr>other)._sa) and self._pid == (<SocketAddr>other)._pid

# ── Pipe event trampoline (GIL-free) ─────────────────────────────────────────

cdef void _pipe_trampoline(nng_pipe pipe,
                           nng_pipe_ev ev,
                           void *arg) noexcept nogil:
    """Push pipe event into the GIL-free queue; send sentinel to wake dispatcher.

    The socket id is captured here, while the pipe handle is still valid.
    REM_POST events arrive after nng has already destroyed the pipe handle,
    so calling nng_pipe_socket() at dispatch time would return an invalid id.
    """
    cdef PipeCollection* pipe_collection = <PipeCollection*>arg
    pipe_collection.handle_event(pipe, ev)
    _DISPATCH_QUEUE.push(0)   # sentinel – wakes dispatcher; id=0 is ignored


# ── PipeStatus ────────────────────────────────────────────────────────────────

class PipeStatus(_enum.IntEnum):
    """Lifecycle state of a :class:`Pipe`.

    * ``ADDING``  – the pipe has been created and negotiated (ADD_PRE event).
      The :attr:`Socket.on_new_pipe` callback fires at this point.
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
    as arguments to callbacks registered with :attr:`Socket.on_new_pipe`.
    The ``Pipe`` object is **non-owning** — closing it kicks the connection;
    the underlying socket is unaffected.
    """

    # Underlying handle, sharing the reference with the parent SocketHandle's PipeCollection.
    cdef shared_ptr[PipeHandle] _handle

    # Last known status (for change callbacks)
    cdef DCGMutex _status_mutex
    cdef uint32_t _last_known_status
    cdef object _change_callback

    # Weak references to parent objects
    cdef object _weak_socket_ref
    cdef object _weak_dialer_ref
    cdef object _weak_listener_ref

    cdef object __weakref__

    def __init__(self):
        raise TypeError("Pipes cannot be instantiated directly")

    @staticmethod
    cdef Pipe create(shared_ptr[PipeHandle] h, Socket sock, Dialer dialer, Listener listener):
        cdef Pipe obj = Pipe.__new__(Pipe)
        obj._handle = h
        obj._weak_socket_ref = _weakref(sock)
        obj._weak_dialer_ref = _weakref(dialer) if dialer is not None else None
        obj._weak_listener_ref = _weakref(listener) if listener is not None else None
        obj._last_known_status = h.get().get_status()
        _PIPE_REGISTRY[h.get().get_id()] = obj
        return obj

    @property
    def id(self) -> int:
        """Numeric pipe ID."""
        if not self._handle:
            raise RuntimeError("Invalid Pipe")
        return self._handle.get().get_id()

    def __index__(self): return self._handle.get().get_id()
    def __hash__(self):  return self._handle.get().get_id()
    def __eq__(self, other):
        if not isinstance(other, Pipe):
            return NotImplemented
        return self._handle.get().get_id() == (<Pipe>other)._handle.get().get_id()
    def __ne__(self, other):
        if not isinstance(other, Pipe):
            return NotImplemented
        return self._handle.get().get_id() != (<Pipe>other)._handle.get().get_id()

    @property
    def status(self) -> PipeStatus:
        """Current :class:`PipeStatus` of this pipe."""
        if not self._handle:
            raise RuntimeError("Invalid Pipe")

        # We set the closed status a bit earlier than the callback informs us.
        if self._last_known_status == NNG_PIPE_EV_REM_POST:
            return PipeStatus.REMOVED

        return PipeStatus(self._handle.get().get_status())

    @property
    def on_status_change(self):
        """Callback fired when this pipe transitions to ACTIVE or REMOVED."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._status_mutex)

        return self._change_callback

    @on_status_change.setter
    def on_status_change(self, cb):
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._status_mutex)

        if not callable(cb) and cb is not None:
            raise ValueError("on_status_change must be set to a callable or None")

        self._change_callback = cb

    @property
    def socket(self):
        """The owning :class:`Socket`, or ``None`` if no longer tracked."""
        return self._weak_socket_ref() if self._weak_socket_ref is not None else None

    @property
    def dialer(self):
        """The :class:`Dialer` that created this pipe, or ``None``."""
        return self._weak_dialer_ref() if self._weak_dialer_ref is not None else None

    @property
    def listener(self):
        """The :class:`Listener` that accepted this pipe, or ``None``."""
        return self._weak_listener_ref() if self._weak_listener_ref is not None else None

    def get_peer_addr(self) -> SocketAddr:
        """Remote peer address."""
        if not self._handle:
            raise RuntimeError("Invalid Pipe")

        cdef nng_sockaddr sa
        check_err(self._handle.get().peer_addr(sa))
        return SocketAddr.create(sa, self._handle.get().get_peer_pid())

    def get_self_addr(self) -> SocketAddr:
        """Local address."""
        if not self._handle:
            raise RuntimeError("Invalid Pipe")

        cdef nng_sockaddr sa
        check_err(self._handle.get().self_addr(sa))
        return SocketAddr.create(sa, _getpid())

    @property
    def tcp_nodelay(self) -> bool:
        """``True`` when TCP_NODELAY (Nagle disabled) was active at connection time.

        Captured once at pipe creation.  Always ``False`` for non-TCP pipes.
        """
        if not self._handle:
            raise RuntimeError("Invalid Pipe")
        return bool(self._handle.get().get_nodelay())

    @property
    def tcp_keepalive(self) -> bool:
        """``True`` when TCP keep-alive probes were enabled at connection time.

        Captured once at pipe creation.  Always ``False`` for non-TCP pipes.
        """
        if not self._handle:
            raise RuntimeError("Invalid Pipe")
        return bool(self._handle.get().get_keepalive())

    @property
    def tls_verified(self) -> bool:
        """``True`` when the peer's TLS certificate was successfully verified.

        Returns ``False`` for non-TLS pipes or when peer verification was not
        required / did not take place.  Captured once at pipe creation.
        """
        if not self._handle:
            raise RuntimeError("Invalid Pipe")
        return bool(self._handle.get().get_tls_verified())

    @property
    def tls_peer_cn(self) -> str | None:
        """Common name (CN) from the peer's TLS certificate, or ``None``.

        Returns ``None`` for non-TLS pipes or when no certificate was
        presented.  Captured once at pipe creation.
        """
        if not self._handle:
            raise RuntimeError("Invalid Pipe")
        cdef const char *s = self._handle.get().get_tls_peer_cn()
        if s is NULL:
            return None
        return s.decode("utf-8", errors="replace")

    def get_peer_cert(self) -> TlsCert | None:
        """Return the peer's TLS certificate, or ``None`` for non-TLS pipes.

        The certificate data is captured at pipe creation time and is
        fully independent of the pipe's lifetime.  The returned
        :class:`TlsCert` is an **owning** object; it is safe to keep it
        after the pipe is closed.

        Returns:
            An owning :class:`TlsCert`, or ``None`` if this is not a TLS
            pipe or no certificate was presented.
        """
        if not self._handle:
            raise RuntimeError("Invalid Pipe")

        cdef const uint8_t *buf = NULL
        cdef size_t sz = 0
        if not self._handle.get().get_peer_cert_der(&buf, &sz):
            return None

        # Parse the cached DER bytes into a new TlsCert.
        return TlsCert.from_der(PyBytes_FromStringAndSize(<const char*>buf, sz))

    cdef object handle_status_change(self):
        """Check the current status of this pipe and return the callback to call if any."""
        # Fail silently on invalid pipe
        if not self._handle:
            return None

        # Fire the callback on change
        return self._handle_status_change_internal(self._handle.get().get_status())

    cdef object _handle_status_change_internal(self, uint32_t new_status):
        """Set the new status, and if it has changed return the callback to call if any.
        
        Note no new callback will be fired after REMOVED is reached. We
        may set internally REMOVED before the pipe is reported removed
        by NNG.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._status_mutex)

        # Fail silently on invalid pipe
        if not self._handle:
            return None

        # Request to fire the callback on change
        if new_status != self._last_known_status \
           and self._last_known_status != NNG_PIPE_EV_REM_POST:
            self._last_known_status = new_status
            cb = self.on_status_change

            if cb is not None:
                return cb
        return None

    def close(self) -> None:
        """Forcibly close this connection.

        The underlying transport is terminated immediately.  Dialers attempt to
        reconnect; listeners wait for a new inbound connection.  In-flight
        sends/receives on this pipe fail immediately.

        If a change callback is registered it fires when the close completes.

        If a pipe is closed several times, only the first call has an effect.
        Subsequent calls are no-ops.
        """
        if not self._handle:
            raise RuntimeError("Invalid Pipe")

        cdef int err = self._handle.get().close()

        # Ignore expected errors from closing an already-closed pipe; raise unexpected ones.
        if err not in (0, NNG_ECLOSED, NNG_ENOENT):
            check_err(err)

        # Handle status change immediately
        cb = self._handle_status_change_internal(NNG_PIPE_EV_REM_POST)
        if cb is not None:
            cb() # We can affort to raise on error in this function.

    def __repr__(self) -> str:
        if not self._handle:
            raise RuntimeError("Invalid Pipe")

        return f"Pipe(id={self._handle.get().get_id()}, status={self.status.name})"


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
    cdef object   _on_new_pipe_cb   # callable(Pipe) | None (ADD_PRE)

    # For polling. Lazy init.
    cdef int _fd_recv
    cdef int _fd_send

    # Per-loop AIO dispatch managers; cleared on close.
    cdef DCGMutex _aio_mgr_mutex
    cdef object _aio_managers  # WeakKeyDictionary[loop, _AioAsyncManager]

    cdef object __weakref__

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.
        self._dialers = []
        self._listeners = []
        self._pipes = []
        self._on_new_pipe_cb = None
        self._fd_recv = -1
        self._fd_send = -1
        self._aio_managers = _WeakKeyDictionary()

    cdef int _setup_pipe_tracking(self) except *:
        """Register the GIL-free trampoline and add socket to the registry.

        Must be called from each concrete subclass __cinit__ immediately after
        self._handle is assigned (i.e. after nng_xxx_open succeeds).
        """
        cdef nng_socket raw = self._handle.get().raw()
        _SOCKET_REGISTRY[nng_socket_id(raw)] = self
        # Note: pipe_collection remains alive as long as the SocketHandle is alive,
        # and _pipe_trampoline won't be called after SocketHandle is destroyed,
        # so it's safe to capture a pointer to the pipe_collection here.
        check_err(nng_pipe_notify(raw, NNG_PIPE_EV_ADD_PRE,  _pipe_trampoline, <void*>self._handle.get().pipe_collection()))
        check_err(nng_pipe_notify(raw, NNG_PIPE_EV_ADD_POST, _pipe_trampoline, <void*>self._handle.get().pipe_collection()))
        check_err(nng_pipe_notify(raw, NNG_PIPE_EV_REM_POST, _pipe_trampoline, <void*>self._handle.get().pipe_collection()))
        return 0

    # __dealloc__ intentionally omitted: shared_ptr[SocketHandle] destructor
    # calls SocketHandle::~SocketHandle() -> nng_socket_close() when ref-count hits 0.

    # ── Pipe management cdef methods ──────────────────────────────────────────

    cdef int update_pipes(self):
        """Check the inner PipeCollection for any change to pipes and process them"""
        if not self._handle:
            return 0

        # Important: holding the lock before had_events. This
        # is to ensure we return after any other update_pipes running
        # in another thread. We want to return when the most up to
        # date pipe status is reflected.
        # This allows for instance dialer.start(block=True) to
        # always have dialer.pipe not None when it returns (unless disconnect).
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)

        if not self._handle.get().pipe_collection().had_events():
            # Fast path: no events have been handled since the last update, so no changes to process.
            return 0

        # Retrieve all the current pipe handles
        cdef vector[shared_ptr[PipeHandle]] pipe_handles = self._handle.get().pipe_collection().get_pipes()

        # Retrieve current Pipe instances
        cdef list previous_pipes = self._pipes

        # Retrieve the new pipes list mirroring pipe_handles
        cdef list new_pipes = []
        cdef list still_active_pipes = []
        cdef Pipe pipe
        cdef int i = 0
        cdef int j

        # Current_pipes and pipe_handles are in the same order
        for pipe in previous_pipes:
            if i >= pipe_handles.size():
                break
            if pipe._handle.get().get_id() == pipe_handles[i].get().get_id():
                still_active_pipes.append(pipe)
                i += 1

        # Add novel pipes
        for j in range(i, pipe_handles.size()):
            new_pipes.append(self._create_pipe(pipe_handles[j]))

        # Update the pipe list
        self._pipes = still_active_pipes + new_pipes

        # Retrieve the pipe creation callback
        pipe_creation_cb = self._on_new_pipe_cb

        # Update pipe status now, and call status callback later
        cdef list pipe_update_cbs = []
        for pipe in previous_pipes: # The other pipes are new and cannot have callbacks yet
            cb = pipe.handle_status_change()
            if cb is not None:
                pipe_update_cbs.append(cb)

        # Note: due to the remark at the start of update_pipes, we must do all
        # the update processing in a single lock (no relock after this in this function)
        lock.unlock()

        # Call the status change callbacks for existing pipes outside the lock
        for cb in pipe_update_cbs:
            try:
                cb()
            except Exception:
                _print_exc()

        # Call the callbacks for new pipes outside the lock
        if pipe_creation_cb is not None:
            for pipe in new_pipes:
                try:
                    pipe_creation_cb(pipe)
                except Exception:
                    _print_exc()

        return 0

    cdef Pipe _create_pipe(self, shared_ptr[PipeHandle] h):
        """Create a Pipe object for the given handle, and find its dialer/listener if any."""
        # Find dialer and listener for this pipe (if any).
        cdef nng_dialer d = h.get().get_dialer()
        cdef nng_listener l = h.get().get_listener()
        cdef Dialer dialer = None
        cdef Listener listener = None
        if d.id != 0:
            for dl in self._dialers:
                if (<Dialer>dl)._handle.id() == d.id:
                    dialer = dl
                    break
        if l.id != 0:
            for ln in self._listeners:
                if (<Listener>ln)._handle.id() == l.id:
                    listener = ln
                    break

        return Pipe.create(h, self, dialer, listener)

    cdef _AioAsyncManager get_aio_manager(self):
        """Return (creating if needed) the AIO manager for the running event loop.

        Returns:
            The :class:`_AioAsyncManager` bound to the current asyncio loop.

        Raises:
            RuntimeError: If there is no running event loop.
        """
        cdef object loop = _get_running_loop()
        if loop is None:
            raise RuntimeError("No running event loop")

        cdef unique_lock[DCGMutex] mgr_lock
        lock_gil_friendly(mgr_lock, self._aio_mgr_mutex)

        # Fast path: manager already exists for this loop.
        cdef object mgr = self._aio_managers.get(loop)
        if mgr is None:
            # Slow path: first asend/arecv on this loop – create and cache the manager.
            mgr = _AioAsyncManager.create(loop)
            self._aio_managers[loop] = mgr
        return <_AioAsyncManager>mgr

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
        lock_gil_friendly(lock, self._pipe_mutex)

        if not self._handle or not self._handle.get().is_open():
            return  # already closed; no-op
    
        # Clear pipe tracking before closing so callbacks cannot fire after close.
        self._on_new_pipe_cb = None
        pipes = self._pipes.copy()  # copy for safe iteration after releasing lock
        self._pipes.clear()
        self._handle.get().pipe_collection().clear()
        lock.unlock()
        with nogil:
            self._handle.get().close()

        # fire REMOVED callbacks for all pipes
        cdef Pipe pipe
        for pipe in pipes:
            try:
                pipe.handle_status_change()
            except Exception:
                _print_exc()

        # Release every AIO manager so add_reader / drain tasks are cleaned up.
        cdef unique_lock[DCGMutex] mgr_lock
        lock_gil_friendly(mgr_lock, self._aio_mgr_mutex)
        cdef _AioAsyncManager aio_mgr
        for aio_mgr_obj in list(self._aio_managers.values()):
            try:
                (<_AioAsyncManager>aio_mgr_obj).release()
            except Exception:
                _print_exc()
        self._aio_managers.clear()
        mgr_lock.unlock()

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
        """Receive timeout in ms (-1 = infinite, 0 = non-blocking).
        
        This affects all `recv`/`arecv`/`submit_recv` operations on this socket, regardless of pipe,
        starting from the time this option is modified.

        Contexts inherit from this parameter at creation.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), NNG_OPT_RECVTIMEO, &v))
        return v

    @recv_timeout.setter
    def recv_timeout(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), NNG_OPT_RECVTIMEO, ms))

    @property
    def send_timeout(self) -> int:
        """Send timeout in ms (-1 = infinite, 0 = non-blocking).

        This affects all `send`/`asend`/`submit_send` operations on this socket, regardless of pipe,
        starting from the time this option is modified.

        Contexts inherit from this parameter at creation.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), NNG_OPT_SENDTIMEO, &v))
        return v

    @send_timeout.setter
    def send_timeout(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), NNG_OPT_SENDTIMEO, ms))

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
                   recv_max_size=None,
                   tcp_nodelay=None,
                   tcp_keepalive=None,
                   tcp_local_addr=None) -> Dialer:
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

        Note the self address of a dialer after it has connected to a peer
        can be retrieved from the dialer's active pipe (``dialer.pipe.get_self_addr()``),
        and may change at every reconnection (in particular the port).

        Parameters
        ----------
        url:
            Connection URL.  Supported schemes:

            * ``tcp://host:port`` — TCP/IP.  *host* is a DNS name or an
              IPv4/IPv6 address literal.  IPv6 addresses must be bracketed:
              ``tcp://[::1]:5555``.  Use ``tcp4://`` or ``tcp6://`` to force
              IPv4-only or IPv6-only DNS resolution respectively.  The port
              is required and must be non-zero (you are connecting *to* a
              port, not binding one).
            * ``ipc:///path/to/socket`` — Unix-domain socket (POSIX only).
            * ``inproc://name`` — in-process transport (same process only).
            * ``ws://host:port/path`` — WebSocket.
            * ``wss://host:port/path`` — WebSocket over TLS.
        tls:
            A :class:`TlsConfig` for TLS connections.
        reconnect_min_ms:
            Minimum reconnect interval in ms (default: 100).  After a
            disconnect the dialer waits at least this long before retrying.
        reconnect_max_ms:
            Maximum reconnect interval in ms.  Retries use exponential
            back-off capped at this value.  ``0`` (default) means use
            *reconnect_min_ms* as a fixed interval.
        recv_max_size:
            Maximum incoming message size in bytes (Default: 0 = no limit).
            Messages beyond the limit will be discarded. Use this to
            protect against misbehaving peers sending huge messages.
        tcp_nodelay:
            Set ``NNG_OPT_TCP_NODELAY`` on the TCP connection.  ``True``
            disables Nagle's algorithm (lower latency, potentially more
            packets).  ``False`` enables it (fewer, larger packets).
            ``None`` (default) leaves the OS/nng default in place (True).
            Has no effect for non-TCP transports.
        tcp_keepalive:
            Set ``NNG_OPT_TCP_KEEPALIVE`` on the TCP connection.  ``True``
            enables TCP keep-alive probes, which detect dead connections even
            when no application data is flowing.  ``None`` (default) leaves
            the OS/nng default in place (False).  Has no effect for non-TCP transports.
        tcp_local_addr:
            Source IP address string (e.g. ``"192.168.1.5"`` or ``"::1"``)
            to bind the outgoing connection to. Only useful on multi-homed hosts
            to select a specific network interface.  The port is ignored —
            the OS assigns an ephemeral source port automatically.  ``None``
            (default) lets the OS choose the source address.  Only supported
            for TCP (``tcp://`` scheme).

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
        ValueError
            If *tcp_local_addr* is not a valid IPv4 or IPv6 address string.
        """
        self._check()
        cdef bytes b = _encode_url(url)
        cdef int err = 0
        cdef nng_sockaddr sa
        cdef DialerHandle dh = make_dialer(self._handle, b, err)
        check_err(err)
        # Options: if any check_err raises, dh's destructor auto-calls nng_dialer_close.
        if tls is not None:
            check_err(dh.set_tls((<TlsConfig?>tls)._handle))
        if reconnect_min_ms is not None:
            check_err(dh.set_reconnect_time_min_ms(reconnect_min_ms))
        if reconnect_max_ms is not None:
            check_err(dh.set_reconnect_time_max_ms(reconnect_max_ms))
        if tcp_nodelay is not None:
            check_err(dh.set_nodelay(<cpp_bool>tcp_nodelay))
        if tcp_keepalive is not None:
            check_err(dh.set_keepalive(<cpp_bool>tcp_keepalive))
        if tcp_local_addr is not None:
            sa = _ip_str_to_sockaddr(str(tcp_local_addr))
            check_err(dh.set_local_addr(&sa))
        if recv_max_size is not None:
            check_err(dh.set_recv_max_size(recv_max_size))
        cdef Dialer obj = Dialer.create(move(dh), self)
        self._dialers.append(obj)
        return obj

    def add_listener(self, url, *,
                     tls=None,
                     recv_max_size=None,
                     tcp_nodelay=None,
                     tcp_keepalive=None) -> Listener:
        """Create, configure, and register an inbound :class:`Listener` for *url*.

        The listener is created and all options applied, but it does **not**
        start accepting connections yet.  Call :meth:`Listener.start` on the
        returned object to begin listening::

            lst = sock.add_listener("tcp://0.0.0.0:5555")
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
            Address to bind to.  Supported schemes:

            * ``tcp://host:port`` — TCP/IP.  Use ``0.0.0.0`` (or simply ``:``)
              to accept on all local IPv4 interfaces, ``[::]`` for all local
              IPv6 interfaces, or an explicit IP to restrict to a single interface
              (e.g. ``tcp://127.0.0.1:5555`` for loopback-only).  Use ``tcp4://``
              or ``tcp6://`` to force a specific IP version.  Port ``0`` lets
              the OS assign an ephemeral port; retrieve the actual port from
              :attr:`Listener.port` after :meth:`Listener.start` returns.
            * ``ipc:///path/to/socket`` — Unix-domain socket or Windows named pipe.
            * ``inproc://name`` — in-process transport (same process only).
            * ``ws://addr:port/path`` — WebSocket.
            * ``wss://addr:port/path`` — WebSocket over TLS.
        tls:
            A :class:`TlsConfig` for TLS connections.
        recv_max_size:
            Maximum incoming message size in bytes (Default: 0 = no limit).
            Messages beyond the limit will be discarded. Use this to
            protect against misbehaving peers sending huge messages.
        tcp_nodelay:
            Set ``NNG_OPT_TCP_NODELAY`` on accepted TCP connections.  ``True``
            disables Nagle's algorithm (lower latency, potentially more
            packets).  ``None`` (default) leaves the OS/nng default in place (True).
            Has no effect for non-TCP transports.
        tcp_keepalive:
            Set ``NNG_OPT_TCP_KEEPALIVE`` on accepted TCP connections.  ``True``
            enables TCP keep-alive probes.  ``None`` (default) leaves the
            OS/nng default in place (False).  Has no effect for non-TCP transports.

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
            check_err(lh.set_tls((<TlsConfig?>tls)._handle))
        if recv_max_size is not None:
            check_err(lh.set_recv_max_size(recv_max_size))
        if tcp_nodelay is not None:
            check_err(lh.set_nodelay(<cpp_bool>tcp_nodelay))
        if tcp_keepalive is not None:
            check_err(lh.set_keepalive(<cpp_bool>tcp_keepalive))
        cdef Listener obj = Listener.create(move(lh), self)
        self._listeners.append(obj)
        return obj

    # ── Synchronous send / recv ───────────────────────────────────────────

    def send(self, data, *, bint nonblock=False) -> None:
        """Send an :class:`Message`.  Transfers ownership on success.

        Parameters
        ----------
        data:
            The :class:`Message` (or bytes-like) to send.
        nonblock:
            If *True*, raise :exc:`NngAgain` instead of blocking.
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

        # Fast path: non-blocking send delegates to nng_sendmsg directly.
        cdef int rv
        if nonblock:
            with nogil:
                rv = nng_sendmsg(self._handle.get().raw(), ptr, NNG_FLAG_NONBLOCK)
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
                nng_socket_send(self._handle.get().raw(), aio)
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

            # Raise if the send failed;
            check_err(rv)
        finally:
            nng_aio_free(aio)

    def recv(self, *, bint nonblock=False) -> Message:
        """Receive and return a :class:`Message` (zero-copy via buffer protocol).

        Parameters
        ----------
        nonblock:
            If *True*, raise :exc:`NngAgain` if no message is ready.
        """
        self._check()

        # Fast path: non-blocking recv delegates to nng_recvmsg directly.
        cdef int rv
        cdef nng_msg *ptr = NULL
        if nonblock:
            with nogil:
                rv = nng_recvmsg(self._handle.get().raw(), &ptr, NNG_FLAG_NONBLOCK)
            check_err(rv)
            return Message._from_ptr(ptr)

        # Blocking path: use an AIO and poll every 100 ms so that
        # Python signals (Ctrl-C) are processed between polls.
        cdef bint still_busy

        # allocate aio
        cdef nng_aio *aio = NULL
        check_err(nng_aio_alloc(&aio, NULL, NULL))
        try:
            nng_aio_set_timeout(aio, NNG_DURATION_DEFAULT)

            # Submit the recv
            with nogil:
                nng_socket_recv(self._handle.get().raw(), aio)
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

        # Obtain the per-loop AIO manager (created lazily on first call).
        cdef _AioAsyncManager mgr = self.get_aio_manager()

        # Create an aio request via the manager (selects optimal dispatch path).
        cdef _AioSockASync op = mgr.create_aio_for_socket(self._handle)

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
        cdef bint completed = op.submit_socket_send(self._handle.get().raw())

        # Fast-path: if the AIO is already complete (if rep could be sent immediately))
        if completed:
            if nng_aio_busy(op._handle.get().get()):
                nng_aio_wait(op._handle.get().get())
            check_err(nng_aio_result(op._handle.get().get()))
            return

        # Await aio completion
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
        '''
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
        '''

        # Slow path: recv queue was empty; use the async aio machinery.
        # Obtain the per-loop AIO manager (created lazily on first call).
        cdef _AioAsyncManager mgr = self.get_aio_manager()
        cdef _AioSockASync op = mgr.create_aio_for_socket(self._handle)

        # Fetch the future now as get_future is invalid after submission
        future = op.get_future()

        # Submit the operation
        cdef bint completed = op.submit_socket_recv(self._handle.get().raw())

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
        cdef _AioCbSync op = _AioCbSync.create_for_socket_concurrent(self._handle, False)

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
        cdef _AioCbSync op = _AioCbSync.create_for_socket_concurrent(self._handle, True)

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

        # Process any pending pipe events (does call callbacks)
        self.update_pipes()

        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        return list(self._pipes)

    @property
    def on_new_pipe(self):
        """Callback invoked when a new connection is negotiated.

        The callback is called at :attr:`PipeStatus.ADDING` – before the pipe
        is admitted to the socket pool.  Calling :meth:`Pipe.close` inside
        the callback rejects the connection entirely.

        Only one callback is kept; assigning again replaces the previous one.
        Set to ``None`` to disable.

        The callback signature is ``(pipe: Pipe) -> None``.  It is executed on
        the dispatcher thread; keep it short and non-blocking.
        """
        self._check()
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._pipe_mutex)
        return self._on_new_pipe_cb

    @on_new_pipe.setter
    def on_new_pipe(self, callback):
        self._check()
        if not callable(callback) and callback is not None:
            raise ValueError("on_new_pipe must be set to a callable or None")
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
    If a peer disconnects, another peer can connect (which allows for reconnection)

    .. note::
       PAIR does **not** guarantee reliable delivery.  Although back-pressure
       prevents most discards, some topologies can still lose messages.
       Applications that need reliable delivery should use :class:`ReqSocket`
       or add their own acknowledgment layer.

    **Protocol versions**

    The default is Pair v1.  Pass ``v0=True`` for the legacy v0 protocol, which
    lacks hop-count headers and is required when interoperating with older
    implementations such as *libnanomsg* or *mangos*.

    *Warning*: if the peer gets disconnected, messages can be lost.
    For instance if the other peer is already connected to a different
    peer, the connection will be rejected after being accepted for
    a brief moment. Even though the connection will succeed for real after the
    other peer disconnects, the messages sent during the brief moment
    of connection will be lost. In other words, any hello/identity
    first message may have to be resent if the pipe disconnects.

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

    @property
    def recv_buf(self) -> int:
        """Receive buffer depth (number of queued messages) before blocking.
        
        The Pair protocol blocks sending messages on the other side until
        the message can be received successfully into the queue.

        A value of 0 (the default) means no buffering, i.e. any unqueued send
        on the other side is blocked until this side does a receive operation.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_RECVBUF, &v))
        return v

    @recv_buf.setter
    def recv_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_RECVBUF, n))

    @property
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages) before blocking.
        
        When non zero, messages are queues to a send buffer until the
        recepient side can receive them.  When the buffer is full, the sender blocks
        until space is available. 0 means no buffering, i.e. the sender blocks until
        the message can be received.

        The default for Pair is 0.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_SENDBUF, &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_SENDBUF, n))


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

    @property
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages) before dropping messages.
        
        The send queue enables to queue messages, enabling recipients time to
        receive all of them. When the buffer is full, older messages are dropped and
        cannot be received anymore by the subscribers.
        The default is 16 for Pub.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_SENDBUF, &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_SENDBUF, n))


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

    @property
    def recv_buf(self) -> int:
        """Receive buffer depth (number of queued messages) before dropping messages.
    
        If the buffer is full, new messages are dropped according to
        the skip_older_on_full_queue policy.

        The default value is 128.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_RECVBUF, &v))
        return v

    @recv_buf.setter
    def recv_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_RECVBUF, n))

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
        check_err(nng_socket_get_bool(self._handle.get().raw(), NNG_OPT_SUB_PREFNEW, &v))
        return bool(v)

    @skip_older_on_full_queue.setter
    def skip_older_on_full_queue(self, bint skip_older):
        self._check()
        check_err(nng_socket_set_bool(self._handle.get().raw(), NNG_OPT_SUB_PREFNEW, skip_older))

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
        multiple REP peers, or possible packet loss. However it has the drawback
        of causing a pipe connection loss upon receiving a reply of the previous
        request after the resend timer has fired.

        Setting a < 1000ms resend_time requires tuning :attr:`resend_tick` to
        have real effect.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), NNG_OPT_REQ_RESENDTIME, &v))
        return v

    @resend_time.setter
    def resend_time(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), NNG_OPT_REQ_RESENDTIME, ms))

    @property
    def resend_tick(self) -> int:
        """How often (ms) to check for timed-out requests to resend.

        The socket checks for timed-out requests at this interval.
        Lower values add overhead, but are needed for short resend_time values.
        
        The default is 1000 ms.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_socket_get_ms(self._handle.get().raw(), NNG_OPT_REQ_RESENDTICK, &v))
        return v

    @resend_tick.setter
    def resend_tick(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), NNG_OPT_REQ_RESENDTICK, ms))

    def open_context(self) -> ReqContext:
        """Open an independent REQ context on this socket.

        Each context has its own send → receive → send state machine and can
        have a request in-flight on a different pipe at the same time as other
        contexts.  This is the preferred way to pipeline concurrent requests
        without opening multiple sockets.
        """
        self._check()
        return ReqContext.open(self)


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
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages) before blocking.
        
        When non zero, messages are queues to a send buffer until the
        recepient side can receive them.  When the buffer is full, the sender blocks
        until space is available.

        Default (0), means operations are unbuffers, and sending will block
        until a peer is available to receive the message.

        The default for Push is 0.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_SENDBUF, &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_SENDBUF, n))

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
        check_err(nng_socket_get_ms(self._handle.get().raw(), NNG_OPT_SURVEYOR_SURVEYTIME, &v))
        return v

    @survey_time.setter
    def survey_time(self, int ms):
        self._check()
        check_err(nng_socket_set_ms(self._handle.get().raw(), NNG_OPT_SURVEYOR_SURVEYTIME, ms))

    ''' -> recv_buf and send_buf seem incorrectly plugged in Surveyor (incomplete implementation in NNG).
    The return values are incorrect, and the code doesn't seem to have the hooks for a change in value
    to affect anything. Leaving this for now until nng implements it.
    @property
    def recv_buf(self) -> int:
        """Receive buffer depth (number of queued messages) before blocking/dropping.
        
        The blocking or dropping behavior depends on the protocol.

        The default is 128 for Surveyor.

        Note the buffer is not shared and each context get their own.
        
        A value of 0 means no buffering. The socket will drop
        all incoming messages.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check() # NOTE: Surveyor seems to have per context recv_buf. Unsure.
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_RECVBUF, &v))
        return v

    @recv_buf.setter
    def recv_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_RECVBUF, n))

    @property
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages) before blocking.

        The default is 8 for Surveyor.

        A value of 0 means no buffering; the socket will block until the message
        can be sent successfully to at least one pipe.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_SENDBUF, &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_SENDBUF, n))

    '''

    def open_context(self) -> SurveyorContext:
        """Open an independent surveyor context on this socket."""
        self._check()
        return SurveyorContext.open(self)


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


    @property
    def recv_buf(self) -> int:
        """Receive buffer depth (number of queued messages) before dropping.
        
        The blocking or dropping behavior depends on the protocol.

        The default is 16 for Bus
        
        A value of 0 means no buffering. The socket will drop 
        messages unless it is actively listening for them.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_RECVBUF, &v))
        return v

    @recv_buf.setter
    def recv_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_RECVBUF, n))

    @property
    def send_buf(self) -> int:
        """Send buffer depth (number of queued messages) before dropping.

        The default is 16 for Bus.

        A value of 0 means no buffering; the socket will drop messages
        unless the receiver is directly ready to receive them.

        This value must be an integer between 0 and 8192, inclusive.
        """
        self._check()
        cdef int v
        check_err(nng_socket_get_int(self._handle.get().raw(), NNG_OPT_SENDBUF, &v))
        return v

    @send_buf.setter
    def send_buf(self, int n):
        self._check()
        check_err(nng_socket_set_int(self._handle.get().raw(), NNG_OPT_SENDBUF, n))

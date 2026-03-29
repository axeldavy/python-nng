# nng/_listener.pxi – included into _nng.pyx
#
# Listener – wraps nng_listener


cdef class Listener:
    """An inbound endpoint that accepts connections on behalf of a :class:`Socket`.

    Unlike a :class:`Dialer` (which maintains a single outbound connection),
    a ``Listener`` can accept **any number of simultaneous inbound connections**.
    Each accepted connection becomes a *pipe* in the parent socket's pipe pool.

    A single socket can have **multiple listeners**, each bound to a different
    address or transport.  For example, a server can listen on both TCP and IPC
    at the same time, and all clients are handled through the same socket.

    Returned by :meth:`Socket.add_listener` (not yet started; call
    :meth:`start` to begin accepting connections).

    All configuration options (TLS, per-pipe timeouts) are set via
    :meth:`Socket.add_listener` before the listener is created.  Once
    created the listener's configuration is **locked** and
    individual options can no longer be changed.

    .. note::
        The dialer/listener distinction is **orthogonal** to the
        client/server role in the protocol.  For example, a ``REQ`` socket
        may use a listener to accept connections from ``REP`` dialers.

    The listener is closed automatically when its parent socket is closed.
    It is also closed explicitly via :meth:`close` or when used as a
    context manager.

    The properties on this class are **read-only** and exist solely for
    inspection after the fact.

    Special behaviours:
        - Setting port 0 in a tcp url causes the listener to bind to an ephemeral port. The actual port
            can then be retrieved from the listener's address property after start() returns. This is a common
            technique for service discovery when combined with an out-of-band mechanism to share the port number
            with clients (e.g. a well-known "discovery" socket or an external service registry).
    """

    cdef ListenerHandle _handle
    cdef object _weak_socket_ref  # weakref to parent Socket to avoid ref cycles

    cdef object __weakref__

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.

    def __init__(self):
        raise RuntimeError("Listener cannot be instantiated directly. Use Socket.add_listener() instead.")

    @staticmethod
    cdef Listener create(ListenerHandle lh, Socket sock):
        """Take ownership of an already-created ListenerHandle."""
        cdef Listener listener = Listener.__new__(Listener)
        listener._handle = move(lh)
        listener._weak_socket_ref = _weakref(sock)
        return listener

    cdef inline void _check(self) except *:
        if not self._handle.is_open():
            raise NngClosed(NNG_ECLOSED, "Listener is closed")

    def close(self) -> None:
        """Stop accepting new connections and close all pipes this listener created.

        Already-established pipes that are actively transferring data will be
        closed immediately.  The parent socket remains open and any pipes from
        *other* listeners or dialers continue operating normally.

        If the listener is already closed this method is a no-op (no exception
        is raised).
        """
        if self._handle.is_open():
            check_err(self._handle.close())

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @property
    def id(self) -> int:
        """The numeric listener ID, or 0 if the listener has been closed."""
        return self._handle.id()

    @property
    def pipes(self) -> list[Pipe]:
        """The active :class:`Pipe`(s) for this listener."""
        self._check()
        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is None:
            raise NngClosed(NNG_ECLOSED, "Parent socket has been closed")
        pipes = sock.pipes
        results = []
        for p in pipes:
            if p.listener is self:
                results.append(p)
        return results

    @property
    def socket(self) -> Socket:
        """The parent :class:`Socket` for this listener."""
        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is None:
            raise NngClosed(NNG_ECLOSED, "Parent socket has been closed")
        return sock

    # ── Options (read-only; set via Socket.add_listener) ────────────────────

    @property
    def port(self) -> int:
        """The local port this listener is bound to, or 0 if not a TCP listener.

        For TCP listeners, this is the actual port number the listener is bound
        to. For non-TCP listeners, this property always returns 0.

        Note that for TCP listeners configured with port 0, the actual port is
        assigned by the OS at bind time and can be retrieved from this property
        after :meth:`start` returns.
        """
        self._check()
        cdef int port = 0
        check_err(self._handle.get_port(&port))
        return port

    @property
    def tcp_nodelay(self) -> bool:
        """``True`` when TCP_NODELAY (Nagle disabled) is active on this listener.

        This corresponds to the ``NNG_OPT_TCP_NODELAY`` option.  Configured
        at construction time via :meth:`Socket.add_listener`.

        Raises
        ------
        NngClosed
            If the listener has been closed.
        NngNotSupported
            If this listener does not use the TCP transport.
        """
        self._check()
        cdef cpp_bool v = False
        cdef int rv = self._handle.get_nodelay(&v)
        if rv == NNG_ENOENT:
            rv = NNG_ECLOSED
        check_err(rv)
        return bool(v)

    @property
    def tcp_keepalive(self) -> bool:
        """``True`` when TCP keep-alive probes are enabled on this listener.

        This corresponds to the ``NNG_OPT_TCP_KEEPALIVE`` option.  Configured
        at construction time via :meth:`Socket.add_listener`.

        Raises
        ------
        NngClosed
            If the listener has been closed.
        NngNotSupported
            If this listener does not use the TCP transport.
        """
        self._check()
        cdef cpp_bool v = False
        cdef int rv = self._handle.get_keepalive(&v)
        if rv == NNG_ENOENT:
            rv = NNG_ECLOSED
        check_err(rv)
        return bool(v)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Bind to the address and start accepting inbound connections.

        Must be called after :meth:`Socket.add_listener` to actually begin
        accepting connections.  Once started, the listener accepts unlimited
        simultaneous peers until :meth:`close` is called.  The listener's
        configuration is **locked** after this call and cannot be changed.

        Raises
        ------
        NngClosed
            If the listener or socket have been closed.
        NngState
            If the listener has already been started.
        NngAddressInUse
            The address is already in use by another listener.
        NngNoMemory
            Insufficient memory to complete the operation.
        NngError
            Other transport-level errors (e.g. permission denied binding
            to a privileged port).
        """
        self._check()
        cdef int rv
        with nogil:
            rv = self._handle.start()
        # If the parent socket has been closed (nng returns NNG_ENOENT
        # - the listener handle is invalidated when the socket closes), 
        # report that as NngClosed instead of a more generic NngError.
        if rv == NNG_ENOENT:
            rv = NNG_ECLOSED
        check_err(rv)

        # Not needed but shouldn't hurt.
        cdef Socket sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is not None:
            sock.update_pipes()

    def __repr__(self) -> str:
        return f"Listener(id={self._handle.id() if self._handle.is_open() else 0})"

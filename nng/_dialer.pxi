# nng/_dialer.pxi – included into _nng.pyx
#
# Dialer  – wraps nng_dialer

cdef class Dialer:
    """An outbound endpoint that connects a :class:`Socket` to a remote peer.

    Each ``Dialer`` establishes and maintains **exactly one outbound connection**
    (called a *pipe*) to its target address.  If the connection is lost, the
    dialer automatically retries in the background using exponential back-off
    between :attr:`reconnect_min_ms` and :attr:`reconnect_max_ms`.

    A single socket can have **multiple dialers**, each pointing at a different
    address.  All resulting pipes are pooled at the socket level and the
    protocol decides how to distribute messages across them.

    Returned by :meth:`Socket.add_dialer` (not yet started; call :meth:`start`
    to initiate the connection).

    All configuration options (TLS, reconnect intervals, timeouts) are set
    via :meth:`Socket.add_dialer` before the dialer is started.
    The dialer's configuration is **locked** and
    individual options can no longer be changed.

    .. note::
        The dialer/listener distinction is **orthogonal** to the
        client/server role in the protocol.  For example, a ``REP`` socket
        may use a dialer to connect to a ``REQ`` socket's listener.

    The dialer is closed automatically when its parent socket is closed.
    It is also closed explicitly via :meth:`close` or when used as a
    context manager.

    The properties on this class are **read-only** and exist solely for
    inspection after the fact.

    Use as a context manager to ensure the endpoint is closed::

        with sock.add_dialer("tcp://127.0.0.1:5555",
                             reconnect_min_ms=100, reconnect_max_ms=5000) as d:
            d.start()
            ...
    """

    cdef DialerHandle _handle
    cdef object _weak_socket_ref  # weakref to parent Socket to avoid ref cycles

    cdef object __weakref__

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.

    @staticmethod
    cdef Dialer create(DialerHandle dh, Socket sock):
        """Take ownership of an already-created DialerHandle."""
        cdef Dialer dialer = Dialer.__new__(Dialer)
        dialer._handle = move(dh)
        dialer._weak_socket_ref = _weakref(sock)
        return dialer

    cdef inline void _check(self) except *:
        if not self._handle.is_open():
            raise NngClosed(NNG_ECLOSED, "Dialer is closed")

    def close(self) -> None:
        """Close the dialer and tear down its active connection, if any.

        The pipe (connection) associated with this dialer is closed immediately.
        Any in-flight sends or receives on that pipe will fail.  The parent
        socket remains open and its other pipes are unaffected.

        If the dialer is already closed this method is a no-op (no exception
        is raised).
        """
        if self._handle.is_open():
            check_err(self._handle.close())

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @property
    def id(self) -> int:
        """The numeric dialer ID, or 0 if the dialer has been closed."""
        return self._handle.id()

    @property
    def pipe(self) -> Pipe:
        """The active :class:`Pipe` for this dialer, or ``None`` if not connected."""
        self._check()
        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is None:
            raise NngClosed(NNG_ECLOSED, "Parent socket has been closed")
        pipes = sock.pipes
        for p in pipes:
            if p.dialer is self:
                return p
        return None

    @property
    def socket(self) -> Socket:
        """The parent :class:`Socket` for this dialer."""
        sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is None:
            raise NngClosed(NNG_ECLOSED, "Parent socket has been closed")
        return sock

    # ── Options (read-only; set via Socket.add_dialer) ────────────────────

    @property
    def recv_timeout(self) -> int:
        """Per-dialer receive timeout in milliseconds (-1 = infinite, 0 = non-blocking).
        Raises
        ------
        NngClosed
            If the dialer has been closed.
        """
        self._check()
        cdef nng_duration v
        cdef int rv = self._handle.get_recv_timeout_ms(&v)
        if rv == NNG_ENOENT: # see start()
            rv = NNG_ECLOSED
        check_err(rv)
        return v

    @property
    def send_timeout(self) -> int:
        """Per-dialer send timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Raises
        ------
        NngClosed
            If the dialer has been closed.
        """
        self._check()
        cdef nng_duration v
        cdef int rv = self._handle.get_send_timeout_ms(&v)
        if rv == NNG_ENOENT: # see start()
            rv = NNG_ECLOSED
        check_err(rv)
        return v

    @property
    def reconnect_min_ms(self) -> int:
        """Minimum reconnect interval in milliseconds.

        After a connection is lost the dialer waits at least this long before
        the first retry.  Subsequent retries use exponential back-off up to
        :attr:`reconnect_max_ms`.  Configured at construction time via
        :meth:`Socket.add_dialer`.

        Raises
        ------
        NngClosed
            If the dialer has been closed.
        """
        self._check()
        cdef nng_duration v
        cdef int rv = self._handle.get_reconnect_time_min_ms(&v)
        if rv == NNG_ENOENT: # see start()
            rv = NNG_ECLOSED
        check_err(rv)
        return v

    @property
    def reconnect_max_ms(self) -> int:
        """Maximum reconnect interval in milliseconds.

        The dialer will not wait longer than this between retries.  ``0``
        means use :attr:`reconnect_min_ms` as a fixed interval.  Configured
        at construction time via :meth:`Socket.add_dialer`.

        Raises
        ------
        NngClosed
            If the dialer has been closed.
        """
        self._check()
        cdef nng_duration v
        cdef int rv = self._handle.get_reconnect_time_max_ms(&v)
        if rv == NNG_ENOENT: # see start()
            rv = NNG_ECLOSED
        check_err(rv)
        return v

    @property
    def tcp_nodelay(self) -> bool:
        """``True`` when TCP_NODELAY (Nagle disabled) is active on this dialer (Default).

        This corresponds to the ``NNG_OPT_TCP_NODELAY`` option.  Configured
        at construction time via :meth:`Socket.add_dialer`.

        Raises
        ------
        NngClosed
            If the dialer has been closed.
        NngNotSupported
            If this dialer does not use the TCP transport.
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
        """``True`` when TCP keep-alive probes are enabled on this dialer.

        This corresponds to the ``NNG_OPT_TCP_KEEPALIVE`` option.  Configured
        at construction time via :meth:`Socket.add_dialer`.

        Raises
        ------
        NngClosed
            If the dialer has been closed.
        NngNotSupported
            If this dialer does not use the TCP transport.
        """
        self._check()
        cdef cpp_bool v = False
        cdef int rv = self._handle.get_keepalive(&v)
        if rv == NNG_ENOENT:
            rv = NNG_ECLOSED
        check_err(rv)
        return bool(v)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, *, bint block=True) -> None:
        """Initiate the outbound connection.

        Must be called after :meth:`Socket.add_dialer` to actually begin
        connecting.  The dialer will maintain the connection and reconnect
        automatically if it is dropped.  Once started, the dialer's
        configuration is **locked** and cannot be changed.

        Parameters
        ----------
        block:
            If *True* (default) block until the first connection succeeds
            (raises on error).  If *False*, return immediately; nng retries
            in the background even if the initial attempt fails — errors
            such as ``NngConnectionRefused`` will **not** be raised in that
            case, and connection progress is opaque.

        Raises
        ------
        NngClosed
            If the dialer or socket have been closed.
        NngState
            If the dialer has already been started.
        NngConnectionRefused
            The remote peer refused the connection (blocking mode only).
        NngConnectionReset
            The remote peer reset the connection (blocking mode only).
        NngAuthError
            TLS peer authentication or authorization failed (blocking mode only).
        NngNoMemory
            Insufficient memory to complete the operation.
        NngError
            Other transport-level errors (e.g. ``NNG_EUNREACHABLE``,
            ``NNG_EPROTO``).
        """
        self._check()
        cdef int flags = 0 if block else NNG_FLAG_NONBLOCK
        cdef int rv
        with nogil:
            rv = self._handle.start(flags)
        # If the parent socket has been closed (nng returns NNG_ENOENT
        # - the dialer handle is invalidated when the socket closes), 
        # report that as NngClosed instead of a more generic NngError.
        if rv == NNG_ENOENT:
            rv = NNG_ECLOSED
        check_err(rv)

        # Ensure the pipe creation callback spawns right after a blocking start
        cdef Socket sock = self._weak_socket_ref() if self._weak_socket_ref is not None else None
        if sock is not None:
            sock.update_pipes()

    def __repr__(self) -> str:
        return f"Dialer(id={self._handle.id() if self._handle.is_open() else 0})"


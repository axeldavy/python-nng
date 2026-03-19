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
    :meth:`Socket.add_listener` before the listener is started.  Once
    :meth:`start` is called, the listener's configuration is **locked** and
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

    Use as a context manager to stop and clean up the listener::

        with sock.add_listener("tcp://0.0.0.0:5555", recv_timeout=5000) as lst:
            lst.start()
            ...
    """

    cdef ListenerHandle _handle

    cdef object __weakref__

    def __cinit__(self):
        check_nng_init()
        # _handle default-constructed (empty) by Cython's C++ member glue.

    @staticmethod
    cdef Listener create(ListenerHandle lh):
        """Take ownership of an already-created ListenerHandle."""
        cdef Listener listener = Listener.__new__(Listener)
        listener._handle = move(lh)
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

    # ── Options (read-only; set via Socket.add_listener) ────────────────────

    @property
    def recv_timeout(self) -> int:
        """Per-listener receive timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Overrides the socket-level ``recv_timeout`` for pipes accepted by this
        listener.  Configured at construction time via :meth:`Socket.add_listener`.

        Raises
        ------
        NngClosed
            If the listener has been closed.
        """
        self._check()
        cdef nng_duration v
        cdef int rv = self._handle.get_ms(b"recv-timeout", &v)
        if rv == NNG_ENOENT: # see start()
            rv = NNG_ECLOSED
        check_err(rv)
        return v

    @property
    def send_timeout(self) -> int:
        """Per-listener send timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Overrides the socket-level ``send_timeout`` for pipes accepted by this
        listener.  Configured at construction time via :meth:`Socket.add_listener`.

        Raises
        ------
        NngClosed
            If the listener has been closed.
        """
        self._check()
        cdef nng_duration v
        cdef int rv = self._handle.get_ms(b"send-timeout", &v)
        if rv == NNG_ENOENT: # see start()
            rv = NNG_ECLOSED
        check_err(rv)
        return v

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

        # to get pipes visible faster.
        _drain_pipe_events()

    def __repr__(self) -> str:
        return f"Listener(id={self._handle.id() if self._handle.is_open() else 0})"

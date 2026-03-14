# nng/_dialer.pxi – included into _nng.pyx
#
# Dialer  – wraps nng_dialer
# Listener – wraps nng_listener

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
    via :meth:`Socket.add_dialer` before the dialer is started.  The
    properties on this class are **read-only** and exist solely for
    inspection after the fact.

    Use as a context manager to ensure the endpoint is closed::

        with sock.add_dialer("tcp://127.0.0.1:5555",
                             reconnect_min_ms=100, reconnect_max_ms=5000) as d:
            d.start()
            ...
    """

    cdef nng_dialer _d

    def __cinit__(self):
        check_nng_init()
        self._d.id = 0

    @staticmethod
    cdef Dialer _from_id(nng_dialer d):
        cdef Dialer dialer = Dialer.__new__(Dialer)
        dialer._d = d
        return dialer

    cdef inline void _check(self) except *:
        if self._d.id == 0:
            raise NngClosed(NNG_ECLOSED, "Dialer is closed")

    def close(self) -> None:
        """Close the dialer and tear down its active connection, if any.

        The pipe (connection) associated with this dialer is closed immediately.
        Any in-flight sends or receives on that pipe will fail.  The parent
        socket remains open and its other pipes are unaffected.
        """
        if self._d.id != 0:
            check_err(nng_dialer_close(self._d))
            self._d.id = 0

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @property
    def id(self) -> int:
        """The numeric dialer ID, or 0 if the dialer has been closed."""
        return nng_dialer_id(self._d)

    # ── Options (read-only; set via Socket.add_dialer) ────────────────────

    @property
    def recv_timeout(self) -> int:
        """Per-dialer receive timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Overrides the socket-level ``recv_timeout`` for pipes created by this
        dialer.  Configured at construction time via :meth:`Socket.add_dialer`.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_dialer_get_ms(self._d, b"recv-timeout", &v))
        return v

    @property
    def send_timeout(self) -> int:
        """Per-dialer send timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Overrides the socket-level ``send_timeout`` for pipes created by this
        dialer.  Configured at construction time via :meth:`Socket.add_dialer`.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_dialer_get_ms(self._d, b"send-timeout", &v))
        return v

    @property
    def reconnect_min_ms(self) -> int:
        """Minimum reconnect interval in milliseconds.

        After a connection is lost the dialer waits at least this long before
        the first retry.  Subsequent retries use exponential back-off up to
        :attr:`reconnect_max_ms`.  Configured at construction time via
        :meth:`Socket.add_dialer`.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_dialer_get_ms(self._d, b"reconnect-time-min", &v))
        return v

    @property
    def reconnect_max_ms(self) -> int:
        """Maximum reconnect interval in milliseconds.

        The dialer will not wait longer than this between retries.  ``0``
        means use :attr:`reconnect_min_ms` as a fixed interval.  Configured
        at construction time via :meth:`Socket.add_dialer`.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_dialer_get_ms(self._d, b"reconnect-time-max", &v))
        return v

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, *, bint block=True) -> None:
        """Initiate the outbound connection.

        Must be called after :meth:`Socket.add_dialer` to actually begin
        connecting.  The dialer will maintain the connection and reconnect
        automatically if it is dropped.

        Parameters
        ----------
        block:
            If *True* (default) block until the first connection succeeds
            (raises on error).  If *False*, return immediately; nng retries
            in the background even if the initial attempt fails.
        """
        self._check()
        cdef int flags = 0 if block else NNG_FLAG_NONBLOCK
        cdef int rv
        with nogil:
            rv = nng_dialer_start(self._d, flags)
        check_err(rv)

    def __repr__(self) -> str:
        return f"Dialer(id={self._d.id})"


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
    :meth:`Socket.add_listener` before the listener is started.  The
    properties on this class are **read-only** and exist solely for
    inspection after the fact.

    Use as a context manager to stop and clean up the listener::

        with sock.add_listener("tcp://0.0.0.0:5555", recv_timeout=5000) as lst:
            lst.start()
            ...
    """

    cdef nng_listener _l

    def __cinit__(self):
        check_nng_init()
        self._l.id = 0

    @staticmethod
    cdef Listener _from_id(nng_listener l):
        cdef Listener listener = Listener.__new__(Listener)
        listener._l = l
        return listener

    cdef inline void _check(self) except *:
        if self._l.id == 0:
            raise NngClosed(NNG_ECLOSED, "Listener is closed")

    def close(self) -> None:
        """Stop accepting new connections and close all pipes this listener created.

        Already-established pipes that are actively transferring data will be
        closed immediately.  The parent socket remains open and any pipes from
        *other* listeners or dialers continue operating normally.
        """
        if self._l.id != 0:
            check_err(nng_listener_close(self._l))
            self._l.id = 0

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @property
    def id(self) -> int:
        """The numeric listener ID, or 0 if the listener has been closed."""
        return nng_listener_id(self._l)

    # ── Options (read-only; set via Socket.add_listener) ────────────────────

    @property
    def recv_timeout(self) -> int:
        """Per-listener receive timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Overrides the socket-level ``recv_timeout`` for pipes accepted by this
        listener.  Configured at construction time via :meth:`Socket.add_listener`.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_listener_get_ms(self._l, b"recv-timeout", &v))
        return v

    @property
    def send_timeout(self) -> int:
        """Per-listener send timeout in milliseconds (-1 = infinite, 0 = non-blocking).

        Overrides the socket-level ``send_timeout`` for pipes accepted by this
        listener.  Configured at construction time via :meth:`Socket.add_listener`.
        """
        self._check()
        cdef nng_duration v
        check_err(nng_listener_get_ms(self._l, b"send-timeout", &v))
        return v

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Bind and start accepting inbound connections.

        Must be called after :meth:`Socket.add_listener` to actually begin
        accepting connections.  Once started, the listener accepts unlimited
        simultaneous peers until :meth:`close` is called.
        """
        self._check()
        cdef int rv
        with nogil:
            rv = nng_listener_start(self._l, 0)
        check_err(rv)

    def __repr__(self) -> str:
        return f"Listener(id={self._l.id})"

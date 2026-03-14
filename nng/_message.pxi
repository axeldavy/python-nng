# nng/_message.pxi – included into _nng.pyx
#
# Message: zero-copy wrapper around nng_msg*.
#
# Ownership rules:
#   • The Message object owns the nng_msg* and frees it in __dealloc__.
#   • _steal_ptr() transfers ownership to the caller (e.g. to send).
#     After a steal the Message must NOT be used.
#   • _from_ptr() wraps an existing nng_msg* (taking ownership).

cdef class Message:
    """An nng message.

    Supports the buffer protocol – body can be read as a zero-copy
    *memoryview* without copying the underlying nng buffer::

        msg = Message(b"hello")
        view = memoryview(msg)          # zero-copy
        assert bytes(view) == b"hello"
    """

    cdef nng_msg *_ptr
    cdef bint     _owned   # False once ownership has been transferred

    def __cinit__(self, data=None, size_t size=0):
        """Allocate a new message.

        Parameters
        ----------
        data:
            Initial body (bytes / bytearray / buffer). *size* is ignored
            when *data* is provided.
        size:
            Pre-allocate a body of *size* zero bytes.
        """
        cdef int rv
        cdef const unsigned char[::1] mv
        self._owned = True
        self._ptr = NULL

        if data is not None:
            mv   = bytes(data)       # ensure contiguous bytes buffer
            size = len(mv)
            rv   = nng_msg_alloc(&self._ptr, 0)   # alloc empty, then append
            check_err(rv)
            if size > 0:
                rv = nng_msg_append(self._ptr, <const void *>&mv[0], size)
                if rv != 0:
                    nng_msg_free(self._ptr)
                    self._ptr = NULL
                    check_err(rv)
        else:
            rv = nng_msg_alloc(&self._ptr, size)
            check_err(rv)

    @staticmethod
    cdef Message _from_ptr(nng_msg *ptr):
        """Wrap an existing *nng_msg* pointer, taking ownership.

        NOTE: calling Message.__new__(Message) triggers __cinit__ which
        allocates an empty nng_msg*. We free that immediately and replace
        it with *ptr*.
        """
        cdef Message m = Message.__new__(Message)
        # __cinit__ ran with data=None, size=0 → allocated an empty msg.
        # Free it and take ownership of *ptr* instead.
        if m._ptr != NULL:
            nng_msg_free(m._ptr)
        m._ptr   = ptr
        m._owned = True
        return m

    cdef nng_msg *_steal_ptr(self) except NULL:
        """Transfer ownership to the caller.

        The Message must not be used after this call.
        Returns NULL and raises if the message has already been sent.
        """
        if self._ptr == NULL:
            raise NngClosed(NNG_ECLOSED,
                            "Message ownership has already been transferred")
        cdef nng_msg *p = self._ptr
        self._ptr  = NULL
        self._owned = False
        return p

    cdef inline void _check(self) except *:
        if self._ptr == NULL:
            raise NngClosed(NNG_ECLOSED,
                            "Message ownership has been transferred (already sent)")

    def __dealloc__(self):
        if self._owned and self._ptr != NULL:
            nng_msg_free(self._ptr)
            self._ptr = NULL

    # ── Buffer protocol (PEP 3118) ─────────────────────────────────────────
    # Exposes the message *body* as a writable, 1-D byte buffer.

    def __getbuffer__(self, Py_buffer *buf, int flags):
        self._check()
        buf.buf      = nng_msg_body(self._ptr)
        buf.len      = <Py_ssize_t> nng_msg_len(self._ptr)
        buf.obj      = self
        buf.itemsize = 1
        buf.ndim     = 1
        buf.format   = b"B"
        buf.readonly = 0
        buf.shape    = &buf.len
        buf.strides  = &buf.itemsize
        buf.suboffsets = NULL
        buf.internal   = NULL

    def __releasebuffer__(self, Py_buffer *buf):
        pass   # no lock needed; nng_msg is not multi-threaded

    # ── Body ──────────────────────────────────────────────────────────────

    @property
    def body(self) -> memoryview:
        """Zero-copy view of the message body."""
        return memoryview(self)

    @property
    def len(self) -> int:
        """Number of bytes in the message body."""
        self._check()
        return nng_msg_len(self._ptr)

    def to_bytes(self) -> bytes:
        """Copy the message body into a new *bytes* object."""
        self._check()
        cdef size_t n = nng_msg_len(self._ptr)
        return (<unsigned char *> nng_msg_body(self._ptr))[:n]

    def clear(self) -> None:
        """Discard all body bytes."""
        self._check()
        nng_msg_clear(self._ptr)

    def reserve(self, size_t capacity) -> None:
        """Reserve *capacity* bytes in the body buffer (avoids reallocations)."""
        self._check()
        check_err(nng_msg_reserve(self._ptr, capacity))

    # ── Body mutation ──────────────────────────────────────────────────────

    def append(self, data) -> None:
        """Append *data* to the end of the body."""
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(nng_msg_append(self._ptr, &mv[0], len(mv)))

    def insert(self, data) -> None:
        """Prepend *data* to the front of the body."""
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(nng_msg_insert(self._ptr, &mv[0], len(mv)))

    def trim(self, size_t n) -> None:
        """Remove *n* bytes from the front of the body."""
        self._check()
        check_err(nng_msg_trim(self._ptr, n))

    def chop(self, size_t n) -> None:
        """Remove *n* bytes from the end of the body."""
        self._check()
        check_err(nng_msg_chop(self._ptr, n))

    # ── Typed body helpers (network byte-order) ────────────────────────────

    def append_u16(self, uint16_t v) -> None:
        self._check(); check_err(nng_msg_append_u16(self._ptr, v))

    def append_u32(self, uint32_t v) -> None:
        self._check(); check_err(nng_msg_append_u32(self._ptr, v))

    def append_u64(self, uint64_t v) -> None:
        self._check(); check_err(nng_msg_append_u64(self._ptr, v))

    def chop_u16(self) -> int:
        self._check()
        cdef uint16_t v
        check_err(nng_msg_chop_u16(self._ptr, &v))
        return v

    def chop_u32(self) -> int:
        self._check()
        cdef uint32_t v
        check_err(nng_msg_chop_u32(self._ptr, &v))
        return v

    def chop_u64(self) -> int:
        self._check()
        cdef uint64_t v
        check_err(nng_msg_chop_u64(self._ptr, &v))
        return v

    def trim_u16(self) -> int:
        self._check()
        cdef uint16_t v
        check_err(nng_msg_trim_u16(self._ptr, &v))
        return v

    def trim_u32(self) -> int:
        self._check()
        cdef uint32_t v
        check_err(nng_msg_trim_u32(self._ptr, &v))
        return v

    def trim_u64(self) -> int:
        self._check()
        cdef uint64_t v
        check_err(nng_msg_trim_u64(self._ptr, &v))
        return v

    # ── Header ────────────────────────────────────────────────────────────

    @property
    def header(self) -> bytes:
        """Copy of the message header as *bytes*."""
        self._check()
        cdef size_t n = nng_msg_header_len(self._ptr)
        return (<unsigned char *> nng_msg_header(self._ptr))[:n]

    def header_clear(self) -> None:
        self._check()
        nng_msg_header_clear(self._ptr)

    def header_append(self, data) -> None:
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(nng_msg_header_append(self._ptr, &mv[0], len(mv)))

    def header_insert(self, data) -> None:
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(nng_msg_header_insert(self._ptr, &mv[0], len(mv)))

    # ── Dup ───────────────────────────────────────────────────────────────

    def dup(self) -> Message:
        """Return an independent copy of this message."""
        self._check()
        cdef nng_msg *copy
        check_err(nng_msg_dup(&copy, self._ptr))
        return Message._from_ptr(copy)

    # ── Pipe association ──────────────────────────────────────────────────

    @property
    def pipe_id(self) -> int:
        """ID of the pipe this message arrived on (0 if not set)."""
        self._check()
        return nng_msg_get_pipe(self._ptr).id

    def __repr__(self) -> str:
        if self._ptr == NULL:
            return "Message(<transferred>)"
        return f"Message(len={nng_msg_len(self._ptr)})"

    def __len__(self) -> int:
        self._check()
        return nng_msg_len(self._ptr)

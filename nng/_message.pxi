# nng/_message.pxi – included into _nng.pyx
#
# Message: zero-copy wrapper around nng_msg*.
#
# Ownership is managed by a C++ MessageHandle held inside a shared_ptr, so
# Python GC ordering cannot cause a double-free or use-after-free:
#   • __cinit__ is a no-op; the shared_ptr is default-constructed (empty) by
#     Cython's C++ member glue.
#   • __init__ (Python-facing constructor) allocates the message.
#   • _from_ptr() wraps an existing nng_msg* (taking ownership).
#   • _steal_ptr() transfers ownership to nng for send; sets the internal
#     pointer to NULL inside the MessageHandle without touching the shared_ptr.
#   • __dealloc__ is a no-op; the MessageHandle destructor handles freeing.


cdef class Message:
    """An nng message.

    Supports the buffer protocol – body can be read as a zero-copy
    *memoryview* without copying the underlying nng buffer::

        msg = Message(b"hello")
        view = memoryview(msg)          # zero-copy
        assert bytes(view) == b"hello"
    """

    cdef MessageHandle _handle

    def __cinit__(self, data=None, size_t size=0):
        # _handle is default-constructed (empty) by Cython's C++ member glue.
        # Actual allocation is deferred to __init__, allowing _from_ptr() to
        # call __new__ and bypass __init__ without wasting an allocation.
        pass

    def __init__(self, data=None, size_t size=0):
        """Allocate a new message.

        Parameters
        ----------
        data:
            Initial body (bytes / bytearray / buffer). *size* is ignored
            when *data* is provided.
        size:
            Pre-allocate a body of *size* zero bytes.
        """
        cdef int err = 0
        cdef const unsigned char[::1] mv

        if data is not None:
            mv = bytes(data)  # ensure contiguous bytes buffer
            self._handle = MessageHandle.alloc_with_data(
                <const void *>&mv[0], len(mv), err)
        else:
            self._handle = MessageHandle.alloc(size, err)
        check_err(err)

    @staticmethod
    cdef Message _from_ptr(nng_msg *ptr):
        """Wrap an existing *nng_msg* pointer, taking ownership.

        Bypasses __init__ so no allocation is wasted.
        """
        cdef Message m = Message.__new__(Message)
        m._handle = MessageHandle(ptr)
        return m

    cdef nng_msg *_steal_ptr(self) except NULL:
        """Transfer ownership to the caller (e.g. for send).

        The Message must not be used after this call.
        Raises NngClosed if already transferred.
        """
        if not self._handle.is_valid():
            raise NngClosed(NNG_ECLOSED,
                            "Message ownership has already been transferred")
        return self._handle.steal()

    cdef inline void _check(self) except *:
        if not self._handle.is_valid():
            raise NngClosed(NNG_ECLOSED,
                            "Message ownership has been transferred (already sent)")

    # __dealloc__ intentionally omitted: the MessageHandle member is destructed
    # automatically by Cython's C++ member cleanup, which calls
    # MessageHandle::~MessageHandle() → nng_msg_free() when the object is freed.

    # ── Buffer protocol (PEP 3118) ─────────────────────────────────────────
    # Exposes the message *body* as a writable, 1-D byte buffer.

    def __getbuffer__(self, Py_buffer *buf, int flags):
        self._check()
        buf.buf      = self._handle.body_ptr()
        buf.len      = <Py_ssize_t> self._handle.body_len()
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
        return self._handle.body_len()

    def to_bytes(self) -> bytes:
        """Copy the message body into a new *bytes* object."""
        self._check()
        return (<unsigned char *> self._handle.body_ptr())[:self._handle.body_len()]

    def clear(self) -> None:
        """Discard all body bytes."""
        self._check()
        self._handle.clear()

    def reserve(self, size_t capacity) -> None:
        """Reserve *capacity* bytes in the body buffer (avoids reallocations)."""
        self._check()
        check_err(self._handle.reserve(capacity))

    # ── Body mutation ──────────────────────────────────────────────────────

    def append(self, data) -> None:
        """Append *data* to the end of the body."""
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(self._handle.append(&mv[0], len(mv)))

    def insert(self, data) -> None:
        """Prepend *data* to the front of the body."""
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(self._handle.insert(&mv[0], len(mv)))

    def trim(self, size_t n) -> None:
        """Remove *n* bytes from the front of the body."""
        self._check()
        check_err(self._handle.trim(n))

    def chop(self, size_t n) -> None:
        """Remove *n* bytes from the end of the body."""
        self._check()
        check_err(self._handle.chop(n))

    # ── Typed body helpers (network byte-order) ────────────────────────────

    def append_u16(self, uint16_t v) -> None:
        self._check(); check_err(self._handle.append_u16(v))

    def append_u32(self, uint32_t v) -> None:
        self._check(); check_err(self._handle.append_u32(v))

    def append_u64(self, uint64_t v) -> None:
        self._check(); check_err(self._handle.append_u64(v))

    def chop_u16(self) -> int:
        self._check()
        cdef uint16_t v
        check_err(self._handle.chop_u16(&v))
        return v

    def chop_u32(self) -> int:
        self._check()
        cdef uint32_t v
        check_err(self._handle.chop_u32(&v))
        return v

    def chop_u64(self) -> int:
        self._check()
        cdef uint64_t v
        check_err(self._handle.chop_u64(&v))
        return v

    def trim_u16(self) -> int:
        self._check()
        cdef uint16_t v
        check_err(self._handle.trim_u16(&v))
        return v

    def trim_u32(self) -> int:
        self._check()
        cdef uint32_t v
        check_err(self._handle.trim_u32(&v))
        return v

    def trim_u64(self) -> int:
        self._check()
        cdef uint64_t v
        check_err(self._handle.trim_u64(&v))
        return v

    # ── Header ────────────────────────────────────────────────────────────

    @property
    def header(self) -> bytes:
        """Copy of the message header as *bytes*."""
        self._check()
        return (<unsigned char *> self._handle.header_ptr())[:self._handle.header_len()]

    def header_clear(self) -> None:
        self._check()
        self._handle.header_clear()

    def header_append(self, data) -> None:
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(self._handle.header_append(&mv[0], len(mv)))

    def header_insert(self, data) -> None:
        self._check()
        cdef const unsigned char[::1] mv = data
        check_err(self._handle.header_insert(&mv[0], len(mv)))

    # ── Dup ───────────────────────────────────────────────────────────────

    def dup(self) -> Message:
        """Return an independent copy of this message."""
        self._check()
        cdef int err = 0
        cdef Message m = Message.__new__(Message)
        m._handle = self._handle.dup(err)
        check_err(err)
        return m

    # ── Pipe association ──────────────────────────────────────────────────

    @property
    def pipe_id(self) -> int:
        """ID of the pipe this message arrived on (0 if not set)."""
        self._check()
        return self._handle.get_pipe().id

    def __repr__(self) -> str:
        if not self._handle.is_valid():
            return "Message(<transferred>)"
        return f"Message(len={self._handle.body_len()})"

    def __len__(self) -> int:
        self._check()
        return self._handle.body_len()

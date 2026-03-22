# nng/_message.pxi – included into _nng.pyx
#
# Message: zero-copy wrapper around nng_msg*.


from cpython.bytes cimport PyBytes_AsStringAndSize, PyBytes_FromStringAndSize
from cpython.bytearray cimport PyByteArray_Size, PyByteArray_AsString

cdef inline int _nng_msg_from_bytearray(data, nng_msg** out_msg):
    """Helper to create a nng_msg* from a Python bytearray."""
    # Alloc and copy data into the message
    cdef nng_msg *ptr = NULL
    cdef int err = nng_msg_alloc(&ptr, PyByteArray_Size(data))
    if err == 0:
        memcpy(nng_msg_body(ptr), PyByteArray_AsString(data), PyByteArray_Size(data))
    out_msg[0] = ptr
    return err


cdef inline int _nng_msg_from_any_data(data, nng_msg** out_msg):
    """Helper to create a nng_msg* from a Python bytes-like object."""
    if isinstance(data, bytearray):
        return _nng_msg_from_bytearray(data, out_msg)
    # Note bytes is handled in nng_msg_from_data

    # Use a const memory view (works with any object with buffer protocol)
    cdef const unsigned char[::1] mv = data

    # Alloc and copy data into the message
    cdef nng_msg *ptr = NULL
    cdef int err = nng_msg_alloc(&ptr, len(mv))
    if err == 0:
        memcpy(nng_msg_body(ptr), &mv[0], len(mv))
    out_msg[0] = ptr
    return err

cdef inline int nng_msg_from_data(data, nng_msg** out_msg):
    """Helper to create a nng_msg* from a Python bytes-like object."""
    if not isinstance(data, bytes):
        if isinstance(data, str):
            data = data.encode()
            # Passthrough to the bytes case below
        else:
            return _nng_msg_from_any_data(data, out_msg)

    # Fast path when data is bytes.
    cdef char* buf
    cdef Py_ssize_t size
    PyBytes_AsStringAndSize(data, &buf, &size)

    # Alloc and copy data into the message
    cdef nng_msg *ptr = NULL
    cdef int err = nng_msg_alloc(&ptr, size)
    if err == 0:
        memcpy(nng_msg_body(ptr), buf, size)
    out_msg[0] = ptr
    return err

cdef class Message:
    """
    A bytes buffer managed by nng.

    Messages are the unit of data transfer in nng.  They consist of a header
    and a body, which can be edited.  The body supports the buffer protocol
    for zero-copy access to the underlying nng buffer.

    When passing any data to send methods, a Message is internally
    constructed to hold a copy of that data.

    Using Message directly to build your message enables to save on
    a data copy which can make a difference for large message.

    Another usage of Message is to forward message from a socket
    to another. `recv` methods return a Message instance which
    can be directly passed to again to `send` methods. This avoids
    a buffer copy. In this use case, it can be useful to
    append or modify the message before forwarding it. For that use,
    Message provides methods to resize/trim/append data.

    When a Message is sent to a send method (except when send fails),
    the ownership of its content is passed to NNG, and the Message
    cannot be used anymore. `dup()` can be used to create an independent
    copy of the Message if you want to reuse the same content for multiple sends.

    For the reason above, when writing into a Message using a wrapper object
    (for instance memoryview or numpy asarray), you must release the wrapper
    objects before sending the Message, otherwise the send will fail with a
    PermissionError.

    Examples:

    Build a message from bytes and send it through a socket::

        msg = Message(b"hello") # create a message and copies to it "hello"
        sock.send(msg)          # ownership of msg's content is transferred to nng

    Fill as 2D array and send it::

        msg = Message(size=10*10*4)  # pre-allocate a message body of 400 bytes
        arr = np.asarray(msg, shape=(10, 10), dtype=np.float32)
        arr[:] = np.random.rand(10, 10) # fill the message body with random data
        del arr                         # release the numpy array wrapper
        sock.send(msg)  # ownership of msg's content is transferred to nng

    Receive a message and forward it after appending an ID::

        msg = sock1.recv()     # receive a message from sock1
        msg.append_u64(12345)  # append an ID to the message body
        sock2.send(msg)        # forward the same message to sock2 (zero-copy)
    """

    cdef MessageHandle _handle
    cdef DCGMutex _lock
    cdef uint64_t _python_buffer_count # Track of buffer exports

    cdef object __weakref__

    def __cinit__(self):
        self._python_buffer_count = 0
        pass

    def __init__(self, data=None, *, size_t size=0):
        """Allocate a new message.

        Parameters
        ----------
        data:
            Initial body (bytes / bytearray / buffer). *size* is ignored
            when *data* is provided.
            str is accepted, in which case it is encoded as UTF-8.
        size:
            Pre-allocate a body of *size* zero bytes.
        """
        cdef int err = 0
        cdef nng_msg *ptr = NULL

        if data is not None:
            err = nng_msg_from_data(data, &ptr)
            check_err(err)

            self._handle = MessageHandle(ptr)
        elif size >= 0:
            self._handle = MessageHandle.alloc(size, err)
            check_err(err)
        else:
            raise ValueError("size must be non-negative")

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
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        return self._handle.steal()

    cdef inline void _check_validity(self) except *:
        if not self._handle.is_valid():
            raise NngClosed(
                NNG_ECLOSED,
                "The Message is empty. This can occur if you "
                "have already sent the Message, which has as effect "
                "to transferred ownership to nng. If you want to send "
                "the same data multiple times, consider using `.dup()` to "
                "duplicate the message before sending."
            )

    cdef inline void _check_full_ownership(self) except *:
        # Check python is not using the buffer
        if self._python_buffer_count > 0:
            raise PermissionError(
                "Cannot modify or send buffer while locked by another "
                "Python object (memoryview, np.asarray, etc.).\n"
                "Release all such objects first, or use `.dup()` to duplicate "
                "the message before sending."
            )  

        self._check_validity()

    # __dealloc__ intentionally omitted: the MessageHandle member is destructed
    # automatically by Cython's C++ member cleanup, which calls
    # MessageHandle::~MessageHandle() → nng_msg_free() when the object is freed.

    # ── Buffer protocol (PEP 3118) ─────────────────────────────────────────
    # Exposes the message *body* as a writable, 1-D byte buffer.

    def __getbuffer__(self, Py_buffer *buf, int flags):
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

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

        # Track the number of exported buffers to prevent stealing while in use.
        self._python_buffer_count += 1

    def __releasebuffer__(self, Py_buffer *buf):
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        # Track the number of exported buffers to prevent stealing while in use.
        if self._python_buffer_count == 0:
            raise RuntimeError("Buffer count underflow")

        self._python_buffer_count -= 1

    # ── Body ──────────────────────────────────────────────────────────────

    @property
    def body(self) -> memoryview:
        """Zero-copy writeable view of the message body."""
        return memoryview(self)

    def to_bytes(self) -> bytes:
        """Copy the message body into a new *bytes* object."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()
        return PyBytes_FromStringAndSize(<char *> self._handle.body_ptr(), self._handle.body_len())

    def clear(self) -> None:
        """Discard all body bytes (append).

        Resets the body length to zero without releasing the pre-allocated
        capacity. Use this to reuse a message buffer with append calls
        without paying for a new allocation.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        self._handle.clear()

    def reserve(self, size_t capacity) -> None:
        """Ensure the body buffer has at least *capacity* bytes pre-allocated.

        Pre-reserving the total anticipated size avoids many small
        reallocations when building a message incrementally with
        ``append`` / ``insert``.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        check_err(self._handle.reserve(capacity))

    def resize(self, size_t new_size) -> None:
        """Resize the body to *new_size* bytes, truncating or zero-padding as needed."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        cdef size_t old_size = self._handle.body_len()
        check_err(self._handle.resize(new_size))

        # zero init new data
        if new_size > old_size:
            memset(<char *> self._handle.body_ptr() + old_size, 0, new_size - old_size)

    # ── Body mutation ──────────────────────────────────────────────────────

    def append(self, data) -> None:
        """Append *data* to the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        cdef const unsigned char[::1] mv = data
        check_err(self._handle.append(&mv[0], len(mv)))

    def insert(self, data) -> None:
        """Prepend *data* to the front of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        cdef const unsigned char[::1] mv = data
        check_err(self._handle.insert(&mv[0], len(mv)))

    def trim(self, size_t n) -> None:
        """Remove *n* bytes from the front of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        check_err(self._handle.trim(n))

    def chop(self, size_t n) -> None:
        """Remove *n* bytes from the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        check_err(self._handle.chop(n))

    # ── Typed body helpers (network byte-order) ────────────────────────────

    def append_u16(self, uint16_t v) -> None:
        """Append a 16-bit unsigned integer in network byte order to the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        check_err(self._handle.append_u16(v))

    def append_u32(self, uint32_t v) -> None:
        """Append a 32-bit unsigned integer in network byte order to the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        check_err(self._handle.append_u32(v))

    def append_u64(self, uint64_t v) -> None:
        """Append a 64-bit unsigned integer in network byte order to the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()
        check_err(self._handle.append_u64(v))

    def chop_u16(self) -> int:
        """Remove and return a 16-bit unsigned integer in network byte order from the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        cdef uint16_t v
        check_err(self._handle.chop_u16(&v))
        return v

    def chop_u32(self) -> int:
        """Remove and return a 32-bit unsigned integer in network byte order from the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        cdef uint32_t v
        check_err(self._handle.chop_u32(&v))
        return v

    def chop_u64(self) -> int:
        """Remove and return a 64-bit unsigned integer in network byte order from the end of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        cdef uint64_t v
        check_err(self._handle.chop_u64(&v))
        return v

    def trim_u16(self) -> int:
        """Remove and return a 16-bit unsigned integer in network byte order from the front of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        cdef uint16_t v
        check_err(self._handle.trim_u16(&v))
        return v

    def trim_u32(self) -> int:
        """Remove and return a 32-bit unsigned integer in network byte order from the front of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        cdef uint32_t v
        check_err(self._handle.trim_u32(&v))
        return v

    def trim_u64(self) -> int:
        """Remove and return a 64-bit unsigned integer in network byte order from the front of the body."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_full_ownership()

        cdef uint64_t v
        check_err(self._handle.trim_u64(&v))
        return v

    # ── Header ────────────────────────────────────────────────────────────

    @property
    def header(self) -> bytes:
        """Copy of the message header as *bytes*.
        
        The header is a separate byte buffer reserved
        for internal use by nng.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

        return (<unsigned char *> self._handle.header_ptr())[:self._handle.header_len()]

    # ── Dup ───────────────────────────────────────────────────────────────

    def dup(self) -> Message:
        """Return an independent copy of this message."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

        cdef int err = 0
        cdef Message m = Message.__new__(Message)
        m._handle = self._handle.dup(err)
        check_err(err)
        return m

    # ── Pipe association ──────────────────────────────────────────────────

    @property
    def pipe_id(self) -> int:
        """ID of the pipe this message arrived on (0 if not set)."""
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

        return self._handle.get_pipe().id

    @property
    def pipe(self):
        """The :class:`Pipe` this message arrived on, or ``None``.

        Returns ``None`` if:
            - The message was not received over a socket pipe (fresh allocation)
            - The message was received over a pipe that has since been closed.

        Else returns the pipe.
        """
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

        cdef nng_pipe p = self._handle.get_pipe()
        cdef int pipe_id = nng_pipe_id(p)

        # Pipe closed or not set
        if pipe_id == 0:
            return None

        lock.unlock()   # release before touching Python structures
    
        # Fast path: pipe in registry (most common case when pipe is still alive)
        pipe = _PIPE_REGISTRY.get(pipe_id, None)
        if pipe is not None:
            return pipe

        cdef int socket_id = nng_socket_id(nng_pipe_socket(p))

        # Case where the pipe is not yet live
        cdef Socket s = _SOCKET_REGISTRY.get(socket_id, None)
        if s is None:
            return None # If the socket is already closed, the pipe must be closed too, so return None

        # Flush live pipes
        s.update_pipes()

        # Retry
        pipe = _PIPE_REGISTRY.get(pipe_id, None)
        if pipe is not None:
            return pipe

        return None

    def __eq__(self, other) -> bool:
        """True when both messages have identical body bytes (memcmp)."""
        if not isinstance(other, Message):
            return memoryview(self) == other
        cdef Message o = <Message>other
        cdef unique_lock[DCGMutex] lock1, lock2

        cdef lock1_acquired = False
        cdef lock2_acquired = False

        # Try to acquire with the gil
        lock1_acquired = lock1.try_lock()
        lock2_acquired = lock2.try_lock()

        if not lock1_acquired or not lock2_acquired:
            # If we failed to acquire either lock, release any acquired lock and
            # acquire both locks without the gil.
            if lock1_acquired:
                lock1.unlock()
            if lock2_acquired:
                lock2.unlock()
            with nogil:
                # Lock in a consistent order to prevent deadlocks
                if <size_t>&self._lock < <size_t>&o._lock:
                    lock1.lock()
                    lock2.lock()
                else:
                    lock2.lock()
                    lock1.lock()
        self._check_validity()
        o._check_validity()
        return self._handle == o._handle

    def __ne__(self, other) -> bool:
        eq = self.__eq__(other)
        if eq is NotImplemented:
            return eq
        return not eq

    def __repr__(self) -> str:
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        if not self._handle.is_valid():
            return "Message(<transferred>)"
        return f"Message(len={self._handle.body_len()})"

    def __len__(self) -> int:
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

        return self._handle.body_len()

    def __str__(self) -> str:
        cdef unique_lock[DCGMutex] lock
        lock_gil_friendly(lock, self._lock)

        self._check_validity()

        return self.to_bytes().decode()
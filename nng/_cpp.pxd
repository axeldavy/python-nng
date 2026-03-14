# nng/_cpp.pxd
#
# Cython extern declarations for the C++ RAII handle classes in nng/cpp/.
# Imported (cimported) by _nng.pyx so all handle types and smart-pointer
# helpers are visible across all included .pxi files.
#   shared_ptr / make_shared  – SocketHandle only (shared with child objects).
#   unique_ptr / make_unique   – all other handles (single owner).

from libcpp.memory cimport shared_ptr, make_shared, unique_ptr, make_unique
from nng._decls cimport (
    nng_socket, nng_ctx, nng_dialer, nng_listener,
    nng_msg, nng_aio, nng_tls_config, nng_tls_mode,
    nng_pipe, nng_duration
)

from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t, int32_t, int16_t

# ── SocketHandle ─────────────────────────────────────────────────────────────

cdef extern from "nng/cpp/socket.hpp" namespace "nng_cpp" nogil:
    cppclass SocketHandle:
        SocketHandle()
        SocketHandle(nng_socket s)
        int close()
        nng_socket raw()
        bint is_open()

# ── MessageHandle ─────────────────────────────────────────────────────────────

cdef extern from "nng/cpp/message.hpp" namespace "nng_cpp" nogil:
    cppclass MessageHandle:
        MessageHandle()
        MessageHandle(nng_msg* m)
        bint is_valid()
        nng_msg* steal()
        void restore(nng_msg* m)
        # body
        size_t body_len()
        void*  body_ptr()
        void   clear()
        int    reserve(size_t n)
        int    append(const void* data, size_t len)
        int    insert(const void* data, size_t len)
        int    trim(size_t n)
        int    chop(size_t n)
        # typed body helpers
        int append_u16(uint16_t v)
        int append_u32(uint32_t v)
        int append_u64(uint64_t v)
        int chop_u16(uint16_t* v)
        int chop_u32(uint32_t* v)
        int chop_u64(uint64_t* v)
        int trim_u16(uint16_t* v)
        int trim_u32(uint32_t* v)
        int trim_u64(uint64_t* v)
        # header
        size_t header_len()
        void*  header_ptr()
        void   header_clear()
        int    header_append(const void* data, size_t len)
        int    header_insert(const void* data, size_t len)
        # dup / pipe
        MessageHandle dup(int& err)
        nng_pipe get_pipe()
        # static factories (return by value)
        @staticmethod
        MessageHandle alloc(size_t size, int& err)
        @staticmethod
        MessageHandle alloc_with_data(const void* data, size_t len, int& err)

# ── TlsConfigHandle ───────────────────────────────────────────────────────────

cdef extern from "nng/cpp/tls_config.hpp" namespace "nng_cpp" nogil:
    cppclass TlsConfigHandle:
        TlsConfigHandle()
        TlsConfigHandle(nng_tls_config* cfg)
        nng_tls_config* get()
        bint is_valid()
        @staticmethod
        unique_ptr[TlsConfigHandle] alloc(nng_tls_mode mode, int& err)

# ── factory.hpp – cross-handle creation free functions ────────────────────────

cdef extern from "nng/cpp/factory.hpp" namespace "nng_cpp" nogil:
    shared_ptr[ContextHandle] make_context(
        const shared_ptr[SocketHandle]& sock, int& err)
    DialerHandle make_dialer(
        const shared_ptr[SocketHandle]& sock, const char* url, int& err)
    ListenerHandle make_listener(
        const shared_ptr[SocketHandle]& sock, const char* url, int& err)

# ── DialerHandle ──────────────────────────────────────────────────────────────

cdef extern from "nng/cpp/dialer.hpp" namespace "nng_cpp" nogil:
    cppclass DialerHandle:
        DialerHandle()
        DialerHandle(nng_dialer d, shared_ptr[SocketHandle] sock)

        bint is_open()
        int close()
        int id()
        int get_ms(const char* opt, nng_duration* v)
        int set_ms(const char* opt, nng_duration v)
        int set_tls(nng_tls_config* cfg)
        int start(int flags)
        nng_dialer raw()

# ── ListenerHandle ────────────────────────────────────────────────────────────

cdef extern from "nng/cpp/listener.hpp" namespace "nng_cpp" nogil:
    cppclass ListenerHandle:
        ListenerHandle()
        ListenerHandle(nng_listener l, shared_ptr[SocketHandle] sock)

        bint is_open()
        int close()
        int id()
        int get_ms(const char* opt, nng_duration* v)
        int set_ms(const char* opt, nng_duration v)
        int set_tls(nng_tls_config* cfg)
        int start()
        nng_listener raw()

# ── ContextHandle ─────────────────────────────────────────────────────────────

cdef extern from "nng/cpp/context.hpp" namespace "nng_cpp" nogil:
    cppclass ContextHandle:
        ContextHandle()
        ContextHandle(nng_ctx ctx, shared_ptr[SocketHandle] sock)
        int close()
        nng_ctx raw()
        bint is_open()

# ── AioHandle ─────────────────────────────────────────────────────────────────

cdef extern from "nng/cpp/aio.hpp" namespace "nng_cpp" nogil:
    cppclass AioHandle:
        AioHandle()
        AioHandle(nng_aio* a)
        AioHandle(nng_aio* a, shared_ptr[ContextHandle] anchor)
        AioHandle(nng_aio* a, shared_ptr[SocketHandle] anchor)
        void mark_callback_thread()
        nng_aio* get()
        bint is_valid()

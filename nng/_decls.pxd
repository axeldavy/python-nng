# nng/_decls.pxd
#
# Corrected Cython extern declarations for nng v2.
# Manually maintained to fix type errors in the auto-generated nng.pxd:
#   - nng_alloc(size_t)          (was missing the argument)
#   - nng_aio_count → size_t     (was int)
#   - nng_iov.iov_len → size_t   (was int)
#   - nng_msg_len/header_len → size_t (was int)
#   - bool-returning functions use bint
#   - Blocking functions carry nogil
#   - nng_pipe_cb / aio callback pointers are noexcept nogil

from libc.stdint cimport (
    int16_t, int32_t,
    uint8_t, uint16_t, uint32_t, uint64_t,
)
from libc.stddef cimport size_t
from libc.time   cimport tm
from libcpp cimport bool as cpp_bool


cdef extern from "nng/nng.h":
    # ── Opaque types ──────────────────────────────────────────────────────────
    ctypedef struct nng_msg:          pass
    ctypedef struct nng_aio:          pass
    ctypedef struct nng_tls_config:   pass
    ctypedef struct nng_tls_cert_s:   pass
    ctypedef struct nng_url:          pass
    ctypedef struct nng_stat:         pass
    ctypedef struct nng_thread:       pass
    ctypedef struct nng_mtx:          pass
    ctypedef struct nng_cv:           pass

    # ── Constants ─────────────────────────────────────────────────────────────
    const int NNG_MAXADDRLEN
    const int NNG_MAXADDRSTRLEN
    const int NNG_FLAG_NONBLOCK
    const int NNG_DURATION_INFINITE
    const int NNG_DURATION_DEFAULT
    const int NNG_DURATION_ZERO

    # ── Identity types ────────────────────────────────────────────────────────
    ctypedef struct nng_ctx_s:
        uint32_t id
    ctypedef nng_ctx_s nng_ctx

    ctypedef struct nng_dialer_s:
        uint32_t id
    ctypedef nng_dialer_s nng_dialer

    ctypedef struct nng_listener_s:
        uint32_t id
    ctypedef nng_listener_s nng_listener

    ctypedef struct nng_pipe_s:
        uint32_t id
    ctypedef nng_pipe_s nng_pipe

    ctypedef struct nng_socket_s:
        uint32_t id
    ctypedef nng_socket_s nng_socket

    ctypedef int32_t  nng_duration   # milliseconds
    ctypedef uint64_t nng_time       # monotonic ms

    # ── Error codes ───────────────────────────────────────────────────────────
    # Declared as plain int constants to avoid Cython generating
    # __Pyx_PyLong_From_enum__nng_err before the #include (incomplete type).
    ctypedef int nng_err
    int NNG_OK
    int NNG_EINTR
    int NNG_ENOMEM
    int NNG_EINVAL
    int NNG_EBUSY
    int NNG_ETIMEDOUT
    int NNG_ECONNREFUSED
    int NNG_ECLOSED
    int NNG_EAGAIN
    int NNG_ENOTSUP
    int NNG_EADDRINUSE
    int NNG_ESTATE
    int NNG_ENOENT
    int NNG_EPROTO
    int NNG_EUNREACHABLE
    int NNG_EADDRINVAL
    int NNG_EPERM
    int NNG_EMSGSIZE
    int NNG_ECONNABORTED
    int NNG_ECONNRESET
    int NNG_ECANCELED
    int NNG_ENOFILES
    int NNG_ENOSPC
    int NNG_EEXIST
    int NNG_EREADONLY
    int NNG_EWRITEONLY
    int NNG_ECRYPTO
    int NNG_EPEERAUTH
    int NNG_EBADTYPE
    int NNG_ECONNSHUT
    int NNG_ESTOPPED
    int NNG_EINTERNAL
    int NNG_ESYSERR
    int NNG_ETRANERR

    # ── Socket-address types ──────────────────────────────────────────────────
    ctypedef struct nng_sockaddr_inproc:
        uint16_t sa_family
        char     sa_name[128]

    ctypedef struct nng_sockaddr_path:
        uint16_t sa_family
        char     sa_path[128]

    ctypedef struct nng_sockaddr_in6:
        uint16_t sa_family
        uint16_t sa_port
        uint32_t sa_scope
        uint8_t  sa_addr[16]

    ctypedef struct nng_sockaddr_in:
        uint16_t sa_family
        uint16_t sa_port
        uint32_t sa_addr

    ctypedef struct nng_sockaddr_abstract:
        uint16_t sa_family
        uint16_t sa_len
        uint8_t  sa_name[107]

    ctypedef struct nng_sockaddr_storage:
        uint16_t sa_family
        uint64_t sa_pad[16]

    ctypedef nng_sockaddr_path     nng_sockaddr_ipc

    union nng_sockaddr:
        uint16_t              s_family
        nng_sockaddr_ipc      s_ipc
        nng_sockaddr_inproc   s_inproc
        nng_sockaddr_in6      s_in6
        nng_sockaddr_in       s_in
        nng_sockaddr_abstract s_abstract
        nng_sockaddr_storage  s_storage

    ctypedef int nng_sockaddr_family
    int NNG_AF_UNSPEC
    int NNG_AF_INPROC
    int NNG_AF_IPC
    int NNG_AF_INET
    int NNG_AF_INET6
    int NNG_AF_ABSTRACT

    ctypedef struct nng_iov:
        void   *iov_buf
        size_t  iov_len

    # ── Socket ────────────────────────────────────────────────────────────────
    int  nng_socket_close(nng_socket) nogil
    int  nng_socket_id(nng_socket) nogil

    int  nng_socket_set_bool  (nng_socket, const char *, bint)    nogil
    int  nng_socket_set_int   (nng_socket, const char *, int)     nogil
    int  nng_socket_set_size  (nng_socket, const char *, size_t)  nogil
    int  nng_socket_set_ms    (nng_socket, const char *, nng_duration) nogil
    int  nng_socket_set_uint64(nng_socket, const char *, uint64_t) nogil

    int  nng_socket_get_bool  (nng_socket, const char *, cpp_bool *)  nogil
    int  nng_socket_get_int   (nng_socket, const char *, int *)     nogil
    int  nng_socket_get_size  (nng_socket, const char *, size_t *)  nogil
    int  nng_socket_get_ms    (nng_socket, const char *, nng_duration *) nogil
    int  nng_socket_get_uint64(nng_socket, const char *, uint64_t *) nogil

    int  nng_socket_get_recv_poll_fd(nng_socket, int *) nogil
    int  nng_socket_get_send_poll_fd(nng_socket, int *) nogil
    int  nng_socket_proto_id  (nng_socket, uint16_t *) nogil
    int  nng_socket_peer_id   (nng_socket, uint16_t *) nogil
    int  nng_socket_proto_name(nng_socket, const char **) nogil
    int  nng_socket_peer_name (nng_socket, const char **) nogil
    int  nng_socket_raw       (nng_socket, bint *) nogil

    const char* nng_str_sockaddr(const nng_sockaddr *, char *, size_t) nogil
    uint32_t nng_sockaddr_port(const nng_sockaddr *) nogil
    bint nng_sockaddr_equal(const nng_sockaddr *, const nng_sockaddr *) nogil
    uint64_t nng_sockaddr_hash(const nng_sockaddr *) nogil

    # ── Pipe events ───────────────────────────────────────────────────────────
    ctypedef int nng_pipe_ev
    int NNG_PIPE_EV_NONE
    int NNG_PIPE_EV_ADD_PRE
    int NNG_PIPE_EV_ADD_POST
    int NNG_PIPE_EV_REM_POST
    int NNG_PIPE_EV_NUM

    ctypedef void (*nng_pipe_cb)(nng_pipe, nng_pipe_ev, void *) noexcept nogil
    nng_err nng_pipe_notify(nng_socket, nng_pipe_ev, nng_pipe_cb, void *) nogil

    # ── Listen / Dial ─────────────────────────────────────────────────────────
    int nng_listen         (nng_socket, const char *, nng_listener *, int) nogil
    int nng_dial           (nng_socket, const char *, nng_dialer *,  int)  nogil
    int nng_dialer_create  (nng_dialer *, nng_socket, const char *)        nogil
    int nng_listener_create(nng_listener *, nng_socket, const char *)      nogil
    int nng_dialer_start   (nng_dialer, int)   nogil
    int nng_listener_start (nng_listener, int) nogil
    int nng_dialer_close   (nng_dialer)  nogil
    int nng_listener_close (nng_listener) nogil
    int nng_dialer_id      (nng_dialer)  nogil
    int nng_listener_id    (nng_listener) nogil

    # ── Dialer options ────────────────────────────────────────────────────────
    int nng_dialer_set_bool  (nng_dialer, const char *, bint)     nogil
    int nng_dialer_set_int   (nng_dialer, const char *, int)      nogil
    int nng_dialer_set_size  (nng_dialer, const char *, size_t)   nogil
    int nng_dialer_set_uint64(nng_dialer, const char *, uint64_t) nogil
    int nng_dialer_set_string(nng_dialer, const char *, const char *) nogil
    int nng_dialer_set_ms    (nng_dialer, const char *, nng_duration) nogil
    int nng_dialer_set_tls   (nng_dialer, nng_tls_config *)        nogil

    int nng_dialer_get_bool  (nng_dialer, const char *, bint *)     nogil
    int nng_dialer_get_int   (nng_dialer, const char *, int *)      nogil
    int nng_dialer_get_size  (nng_dialer, const char *, size_t *)   nogil
    int nng_dialer_get_uint64(nng_dialer, const char *, uint64_t *) nogil
    int nng_dialer_get_string(nng_dialer, const char *, const char **) nogil
    int nng_dialer_get_ms    (nng_dialer, const char *, nng_duration *) nogil
    int nng_dialer_get_tls   (nng_dialer, nng_tls_config **)        nogil
    int nng_dialer_get_url   (nng_dialer, const nng_url **)         nogil

    # ── Listener options ──────────────────────────────────────────────────────
    int nng_listener_set_bool  (nng_listener, const char *, bint)     nogil
    int nng_listener_set_int   (nng_listener, const char *, int)      nogil
    int nng_listener_set_size  (nng_listener, const char *, size_t)   nogil
    int nng_listener_set_uint64(nng_listener, const char *, uint64_t) nogil
    int nng_listener_set_string(nng_listener, const char *, const char *) nogil
    int nng_listener_set_ms    (nng_listener, const char *, nng_duration) nogil
    int nng_listener_set_tls   (nng_listener, nng_tls_config *)        nogil
    int nng_listener_get_url   (nng_listener, const nng_url **)        nogil

    int nng_listener_get_bool  (nng_listener, const char *, bint *)     nogil
    int nng_listener_get_int   (nng_listener, const char *, int *)      nogil
    int nng_listener_get_size  (nng_listener, const char *, size_t *)   nogil
    int nng_listener_get_uint64(nng_listener, const char *, uint64_t *) nogil
    int nng_listener_get_string(nng_listener, const char *, const char **) nogil
    int nng_listener_get_ms    (nng_listener, const char *, nng_duration *) nogil
    int nng_listener_get_tls   (nng_listener, nng_tls_config **)        nogil

    # ── Error string ──────────────────────────────────────────────────────────
    const char *nng_strerror(nng_err) nogil

    # ── Synchronous send / recv ───────────────────────────────────────────────
    int  nng_send    (nng_socket, const void *, size_t, int) nogil
    int  nng_recv    (nng_socket, void *, size_t *, int)     nogil
    int  nng_sendmsg (nng_socket, nng_msg *, int) nogil
    int  nng_recvmsg (nng_socket, nng_msg **, int) nogil
    void nng_socket_send(nng_socket, nng_aio *) nogil
    void nng_socket_recv(nng_socket, nng_aio *) nogil

    # ── Context ───────────────────────────────────────────────────────────────
    int  nng_ctx_open   (nng_ctx *, nng_socket)    nogil
    int  nng_ctx_close  (nng_ctx)                  nogil
    int  nng_ctx_id     (nng_ctx)                  nogil
    void nng_ctx_recv   (nng_ctx, nng_aio *)       nogil
    int  nng_ctx_recvmsg(nng_ctx, nng_msg **, int) nogil
    void nng_ctx_send   (nng_ctx, nng_aio *)       nogil
    int  nng_ctx_sendmsg(nng_ctx, nng_msg *, int)  nogil

    int nng_ctx_get_bool(nng_ctx, const char *, bint *)     nogil
    int nng_ctx_get_int (nng_ctx, const char *, int *)      nogil
    int nng_ctx_get_size(nng_ctx, const char *, size_t *)   nogil
    int nng_ctx_get_ms  (nng_ctx, const char *, nng_duration *) nogil
    int nng_ctx_set_bool(nng_ctx, const char *, bint)       nogil
    int nng_ctx_set_int (nng_ctx, const char *, int)        nogil
    int nng_ctx_set_size(nng_ctx, const char *, size_t)     nogil
    int nng_ctx_set_ms  (nng_ctx, const char *, nng_duration) nogil

    # ── Memory ────────────────────────────────────────────────────────────────
    void *nng_alloc (size_t)       nogil
    void  nng_free  (void *, size_t) nogil
    char *nng_strdup(const char *) nogil
    void  nng_strfree(char *)      nogil

    # ── AIO ───────────────────────────────────────────────────────────────────
    nng_err nng_aio_alloc  (nng_aio **, void (*)(void *) noexcept nogil, void *) nogil
    void    nng_aio_free   (nng_aio *)         nogil
    void    nng_aio_reap   (nng_aio *)         nogil
    void    nng_aio_stop   (nng_aio *)         nogil
    nng_err nng_aio_result (nng_aio *)         nogil
    size_t  nng_aio_count  (nng_aio *)         nogil
    void    nng_aio_cancel (nng_aio *)         nogil
    void    nng_aio_abort  (nng_aio *, nng_err) nogil
    void    nng_aio_wait        (nng_aio *)              nogil
    bint    nng_aio_wait_until  (nng_aio *, nng_time)    nogil
    bint    nng_aio_busy        (nng_aio *)              nogil
    void    nng_aio_set_msg(nng_aio *, nng_msg *) nogil
    nng_msg *nng_aio_get_msg(nng_aio *)        nogil
    int     nng_aio_set_input (nng_aio *, unsigned int, void *) nogil
    void   *nng_aio_get_input (nng_aio *, unsigned int)         nogil
    int     nng_aio_set_output(nng_aio *, unsigned int, void *) nogil
    void   *nng_aio_get_output(nng_aio *, unsigned int)         nogil
    void    nng_aio_set_timeout(nng_aio *, nng_duration) nogil
    void    nng_aio_set_expire (nng_aio *, nng_time)     nogil
    int     nng_aio_set_iov    (nng_aio *, unsigned int, nng_iov *) nogil
    void    nng_aio_reset  (nng_aio *)         nogil
    void    nng_aio_finish (nng_aio *, nng_err) nogil
    void    nng_sleep_aio  (nng_duration, nng_aio *) nogil

    # ── Messages ──────────────────────────────────────────────────────────────
    int     nng_msg_alloc  (nng_msg **, size_t) nogil
    void    nng_msg_free   (nng_msg *)           nogil
    int     nng_msg_realloc(nng_msg *, size_t)  nogil
    int     nng_msg_reserve(nng_msg *, size_t)  nogil
    size_t  nng_msg_capacity(nng_msg *)          nogil
    void   *nng_msg_header    (nng_msg *)        nogil
    size_t  nng_msg_header_len(nng_msg *)        nogil
    void   *nng_msg_body  (nng_msg *)            nogil
    size_t  nng_msg_len   (nng_msg *)            nogil

    int nng_msg_append(nng_msg *, const void *, size_t) nogil
    int nng_msg_insert(nng_msg *, const void *, size_t) nogil
    int nng_msg_trim  (nng_msg *, size_t) nogil
    int nng_msg_chop  (nng_msg *, size_t) nogil

    int nng_msg_header_append(nng_msg *, const void *, size_t) nogil
    int nng_msg_header_insert(nng_msg *, const void *, size_t) nogil
    int nng_msg_header_trim  (nng_msg *, size_t) nogil
    int nng_msg_header_chop  (nng_msg *, size_t) nogil

    int nng_msg_append_u16(nng_msg *, uint16_t) nogil
    int nng_msg_append_u32(nng_msg *, uint32_t) nogil
    int nng_msg_append_u64(nng_msg *, uint64_t) nogil
    int nng_msg_insert_u16(nng_msg *, uint16_t) nogil
    int nng_msg_insert_u32(nng_msg *, uint32_t) nogil
    int nng_msg_insert_u64(nng_msg *, uint64_t) nogil
    int nng_msg_chop_u16  (nng_msg *, uint16_t *) nogil
    int nng_msg_chop_u32  (nng_msg *, uint32_t *) nogil
    int nng_msg_chop_u64  (nng_msg *, uint64_t *) nogil
    int nng_msg_trim_u16  (nng_msg *, uint16_t *) nogil
    int nng_msg_trim_u32  (nng_msg *, uint32_t *) nogil
    int nng_msg_trim_u64  (nng_msg *, uint64_t *) nogil

    int nng_msg_header_append_u16(nng_msg *, uint16_t) nogil
    int nng_msg_header_append_u32(nng_msg *, uint32_t) nogil
    int nng_msg_header_append_u64(nng_msg *, uint64_t) nogil
    int nng_msg_header_insert_u16(nng_msg *, uint16_t) nogil
    int nng_msg_header_insert_u32(nng_msg *, uint32_t) nogil
    int nng_msg_header_insert_u64(nng_msg *, uint64_t) nogil
    int nng_msg_header_chop_u16  (nng_msg *, uint16_t *) nogil
    int nng_msg_header_chop_u32  (nng_msg *, uint32_t *) nogil
    int nng_msg_header_chop_u64  (nng_msg *, uint64_t *) nogil
    int nng_msg_header_trim_u16  (nng_msg *, uint16_t *) nogil
    int nng_msg_header_trim_u32  (nng_msg *, uint32_t *) nogil
    int nng_msg_header_trim_u64  (nng_msg *, uint64_t *) nogil

    int      nng_msg_dup        (nng_msg **, const nng_msg *) nogil
    void     nng_msg_clear      (nng_msg *) nogil
    void     nng_msg_header_clear(nng_msg *) nogil
    void     nng_msg_set_pipe   (nng_msg *, nng_pipe) nogil
    nng_pipe nng_msg_get_pipe   (const nng_msg *) nogil

    # ── Pipe ──────────────────────────────────────────────────────────────────
    nng_err nng_pipe_get_bool  (nng_pipe, const char *, bint *)     nogil
    nng_err nng_pipe_get_int   (nng_pipe, const char *, int *)      nogil
    nng_err nng_pipe_get_ms    (nng_pipe, const char *, nng_duration *) nogil
    nng_err nng_pipe_get_size  (nng_pipe, const char *, size_t *)   nogil
    nng_err nng_pipe_get_string(nng_pipe, const char *, const char **) nogil
    nng_err nng_pipe_peer_addr (nng_pipe, nng_sockaddr *)           nogil
    nng_err nng_pipe_self_addr (nng_pipe, nng_sockaddr *)           nogil
    nng_err nng_pipe_peer_cert (nng_pipe, nng_tls_cert_s **)        nogil
    nng_err nng_pipe_close     (nng_pipe)                           nogil
    int     nng_pipe_id        (nng_pipe)                           nogil
    nng_socket   nng_pipe_socket  (nng_pipe) nogil
    nng_dialer   nng_pipe_dialer  (nng_pipe) nogil
    nng_listener nng_pipe_listener(nng_pipe) nogil

    # ── Stats ─────────────────────────────────────────────────────────────────
    int          nng_stats_get      (nng_stat **) nogil
    void         nng_stats_free     (nng_stat *)  nogil
    nng_stat    *nng_stat_next      (nng_stat *)  nogil
    nng_stat    *nng_stat_child     (nng_stat *)  nogil
    const char  *nng_stat_name      (nng_stat *)  nogil
    int          nng_stat_type      (nng_stat *)  nogil
    nng_stat    *nng_stat_find      (nng_stat *, const char *) nogil
    uint64_t     nng_stat_value     (nng_stat *)  nogil
    const char  *nng_stat_string    (nng_stat *)  nogil
    const char  *nng_stat_desc      (nng_stat *)  nogil

    # ── Device ────────────────────────────────────────────────────────────────
    nng_err nng_device(nng_socket, nng_socket) nogil

    # ── URL ───────────────────────────────────────────────────────────────────
    nng_err      nng_url_parse   (nng_url **, const char *) nogil
    void         nng_url_free    (nng_url *)                nogil
    nng_err      nng_url_clone   (nng_url **, const nng_url *) nogil
    const char  *nng_url_scheme  (const nng_url *) nogil
    uint32_t     nng_url_port    (const nng_url *) nogil
    const char  *nng_url_hostname(const nng_url *) nogil
    const char  *nng_url_path    (const nng_url *) nogil
    const char  *nng_url_query   (const nng_url *) nogil
    const char  *nng_url_fragment(const nng_url *) nogil

    # ── Misc ──────────────────────────────────────────────────────────────────
    const char *nng_version() nogil
    nng_time    nng_clock()   nogil
    void        nng_msleep(nng_duration) nogil
    uint32_t    nng_random()  nogil

    # ── Init / fini ───────────────────────────────────────────────────────────
    ctypedef struct nng_init_params:
        int16_t num_task_threads
        int16_t max_task_threads
        int16_t num_expire_threads
        int16_t max_expire_threads
        int16_t num_poller_threads
        int16_t max_poller_threads
        int16_t num_resolver_threads
    nng_err nng_init(const nng_init_params *) nogil
    void    nng_fini()                        nogil

    # ── TLS types ─────────────────────────────────────────────────────────────
    ctypedef int nng_tls_auth_mode
    int NNG_TLS_AUTH_MODE_NONE
    int NNG_TLS_AUTH_MODE_OPTIONAL
    int NNG_TLS_AUTH_MODE_REQUIRED

    ctypedef int nng_tls_version
    int NNG_TLS_1_2
    int NNG_TLS_1_3

    ctypedef int nng_tls_mode
    int NNG_TLS_MODE_CLIENT
    int NNG_TLS_MODE_SERVER

    # ── TLS config ────────────────────────────────────────────────────────────
    int  nng_tls_config_alloc      (nng_tls_config **, nng_tls_mode)      nogil
    void nng_tls_config_hold       (nng_tls_config *)                      nogil
    void nng_tls_config_free       (nng_tls_config *)                      nogil
    int  nng_tls_config_server_name(nng_tls_config *, const char *)        nogil
    int  nng_tls_config_ca_chain   (nng_tls_config *, const char *, const char *) nogil
    int  nng_tls_config_own_cert   (nng_tls_config *, const char *, const char *, const char *) nogil
    int  nng_tls_config_key        (nng_tls_config *, const uint8_t *, size_t) nogil
    int  nng_tls_config_pass       (nng_tls_config *, const char *)        nogil
    int  nng_tls_config_auth_mode  (nng_tls_config *, nng_tls_auth_mode)   nogil
    int  nng_tls_config_ca_file    (nng_tls_config *, const char *)        nogil
    int  nng_tls_config_cert_key_file(nng_tls_config *, const char *, const char *) nogil
    int  nng_tls_config_version    (nng_tls_config *, nng_tls_version, nng_tls_version) nogil
    int  nng_tls_config_psk        (nng_tls_config *, const char *, const uint8_t *, size_t) nogil

    # ── TLS cert ──────────────────────────────────────────────────────────────
    nng_err nng_tls_cert_parse_pem(nng_tls_cert_s **, const char *, size_t) nogil
    nng_err nng_tls_cert_parse_der(nng_tls_cert_s **, const uint8_t *, size_t) nogil
    void    nng_tls_cert_der       (nng_tls_cert_s *, uint8_t *, size_t *) nogil
    void    nng_tls_cert_free      (nng_tls_cert_s *)                       nogil
    nng_err nng_tls_cert_subject   (nng_tls_cert_s *, char **)              nogil
    nng_err nng_tls_cert_issuer    (nng_tls_cert_s *, char **)              nogil
    nng_err nng_tls_cert_serial_number(nng_tls_cert_s *, char **)           nogil
    nng_err nng_tls_cert_subject_cn(nng_tls_cert_s *, char **)              nogil
    nng_err nng_tls_cert_not_before(nng_tls_cert_s *, tm *)                 nogil
    nng_err nng_tls_cert_not_after (nng_tls_cert_s *, tm *)                 nogil

    # ── Protocol open functions ───────────────────────────────────────────────
    int nng_bus0_open       (nng_socket *) nogil
    int nng_bus0_open_raw   (nng_socket *) nogil

    int nng_pair0_open      (nng_socket *) nogil
    int nng_pair0_open_raw  (nng_socket *) nogil
    int nng_pair1_open      (nng_socket *) nogil
    int nng_pair1_open_raw  (nng_socket *) nogil
    int nng_pair1_open_poly (nng_socket *) nogil

    int nng_pull0_open      (nng_socket *) nogil
    int nng_pull0_open_raw  (nng_socket *) nogil
    int nng_push0_open      (nng_socket *) nogil
    int nng_push0_open_raw  (nng_socket *) nogil

    int nng_pub0_open       (nng_socket *) nogil
    int nng_pub0_open_raw   (nng_socket *) nogil
    int nng_sub0_open       (nng_socket *) nogil
    int nng_sub0_open_raw   (nng_socket *) nogil
    int nng_sub0_socket_subscribe  (nng_socket, const void *, size_t) nogil
    int nng_sub0_socket_unsubscribe(nng_socket, const void *, size_t) nogil
    int nng_sub0_ctx_subscribe     (nng_ctx, const void *, size_t)    nogil
    int nng_sub0_ctx_unsubscribe   (nng_ctx, const void *, size_t)    nogil

    int nng_rep0_open       (nng_socket *) nogil
    int nng_rep0_open_raw   (nng_socket *) nogil
    int nng_req0_open       (nng_socket *) nogil
    int nng_req0_open_raw   (nng_socket *) nogil

    int nng_respondent0_open    (nng_socket *) nogil
    int nng_respondent0_open_raw(nng_socket *) nogil
    int nng_surveyor0_open      (nng_socket *) nogil
    int nng_surveyor0_open_raw  (nng_socket *) nogil

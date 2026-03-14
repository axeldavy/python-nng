# nng/_tls.pxi – included into _nng.pyx
#
# TlsConfig: wraps nng_tls_config* via a C++ TlsConfigHandle (RAII).
# Lifetime is managed by a shared_ptr so the config cannot be freed before
# any Dialer/Listener that references it.

cdef class TlsConfig:
    """TLS configuration object.

    Pass to :meth:`Dialer.set_tls` or :meth:`Listener.set_tls`.

    Example (mTLS client)::

        cfg = TlsConfig(server=False)
        cfg.server_name("example.com")
        cfg.ca_chain(ca_pem)
        cfg.own_cert(cert_pem, key_pem)
        cfg.auth_mode(NNG_TLS_AUTH_MODE_REQUIRED)
        dialer.set_tls(cfg)
    """

    cdef unique_ptr[TlsConfigHandle] _handle

    def __cinit__(self, bint server=False):
        # _handle default-constructed (empty) by Cython's C++ member glue.
        pass

    def __init__(self, bint server=False):
        check_nng_init()
        cdef int err = 0
        cdef nng_tls_mode mode = <nng_tls_mode>(NNG_TLS_MODE_SERVER if server
                                                else NNG_TLS_MODE_CLIENT)
        self._handle = TlsConfigHandle.alloc(mode, err)
        check_err(err)

    # __dealloc__ intentionally omitted: unique_ptr[TlsConfigHandle] destructor
    # calls TlsConfigHandle::~TlsConfigHandle() → nng_tls_config_free().

    cdef inline void _check(self) except *:
        if not self._handle or not self._handle.get().is_valid():
            raise NngClosed(NNG_ECLOSED, "TlsConfig is closed")

    # ── Hold / free (for use by Dialer / Listener internally) ─────────────
    cdef nng_tls_config *_get_ptr(self) except NULL:
        self._check()
        return self._handle.get().get()

    # ── Configuration ─────────────────────────────────────────────────────

    def server_name(self, name: str) -> TlsConfig:
        """Set the expected server name (SNI + verification)."""
        self._check()
        cdef bytes b = name.encode("utf-8")
        check_err(nng_tls_config_server_name(self._handle.get().get(), b))
        return self

    def ca_chain(self, cert_pem: str, crl_pem: str = None) -> TlsConfig:
        """Supply a CA certificate chain (and optional CRL) in PEM format."""
        self._check()
        cdef bytes bc = cert_pem.encode("utf-8")
        cdef bytes br = crl_pem.encode("utf-8") if crl_pem else None
        check_err(nng_tls_config_ca_chain(
            self._handle.get().get(), bc, <const char *>(br) if br else NULL))
        return self

    def own_cert(self,
                 cert_pem: str,
                 key_pem: str,
                 password: str = None) -> TlsConfig:
        """Supply our own certificate and private key."""
        self._check()
        cdef bytes bc = cert_pem.encode("utf-8")
        cdef bytes bk = key_pem.encode("utf-8")
        cdef bytes bp = password.encode("utf-8") if password else None
        check_err(nng_tls_config_own_cert(
            self._handle.get().get(), bc, bk, <const char *>(bp) if bp else NULL))
        return self

    def ca_file(self, path: str) -> TlsConfig:
        """Load a CA certificate chain from a file."""
        self._check()
        cdef bytes b = path.encode("utf-8")
        check_err(nng_tls_config_ca_file(self._handle.get().get(), b))
        return self

    def cert_key_file(self, path: str, password: str = None) -> TlsConfig:
        """Load a combined cert+key file (PEM format)."""
        self._check()
        cdef bytes bp = path.encode("utf-8")
        cdef bytes bpw = password.encode("utf-8") if password else None
        check_err(nng_tls_config_cert_key_file(
            self._handle.get().get(), bp, <const char *>(bpw) if bpw else NULL))
        return self

    def auth_mode(self, int mode) -> TlsConfig:
        """Set peer verification mode (NNG_TLS_AUTH_MODE_*)."""
        self._check()
        check_err(nng_tls_config_auth_mode(
            self._handle.get().get(), <nng_tls_auth_mode>mode))
        return self

    def version(self, int min_ver, int max_ver) -> TlsConfig:
        """Restrict the allowed TLS versions (NNG_TLS_1_2 / NNG_TLS_1_3)."""
        self._check()
        check_err(nng_tls_config_version(
            self._handle.get().get(),
            <nng_tls_version>min_ver,
            <nng_tls_version>max_ver))
        return self

    def __repr__(self) -> str:
        cdef bint valid = self._handle.get() != NULL and self._handle.get().is_valid()
        return f"TlsConfig({'valid' if valid else 'closed'})"


# ── TLS auth-mode and version constants (re-exported at module level) ─────────
TLS_AUTH_NONE     = NNG_TLS_AUTH_MODE_NONE
TLS_AUTH_OPTIONAL = NNG_TLS_AUTH_MODE_OPTIONAL
TLS_AUTH_REQUIRED = NNG_TLS_AUTH_MODE_REQUIRED
TLS_VERSION_1_2   = NNG_TLS_1_2
TLS_VERSION_1_3   = NNG_TLS_1_3

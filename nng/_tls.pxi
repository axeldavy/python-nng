# nng/_tls.pxi – included into _nng.pyx
#
# TlsConfig: wraps nng_tls_config* via a C++ TlsConfigHandle (RAII).
# Lifetime is managed by a shared_ptr so the config cannot be freed before
# any Dialer/Listener that references it.

cdef class TlsConfig:
    """TLS configuration object for securing nng connections.

    Pass as the ``tls=`` argument to :meth:`Socket.add_dialer` or
    :meth:`Socket.add_listener`.

    A single :class:`TlsConfig` may be shared across multiple dialers and
    listeners.  However, once a configuration is attached to a service for
    the first time, it becomes **read-only**: any further attempt to modify
    it (call any method on it) will raise an error.

    Example (mTLS client)::

        cfg = TlsConfig(server=False)    # client mode (default)
        cfg.server_name("example.com")   # SNI + cert hostname check
        cfg.ca_chain(ca_pem)             # CA to validate the server
        cfg.own_cert(cert_pem, key_pem)  # client certificate for mTLS
        sock.add_dialer("tls+tcp://example.com:5555", tls=cfg).start()

    Example (TLS server)::

        cfg = TlsConfig(server=True)
        cfg.cert_key_file("/etc/ssl/server.pem")
        sock.add_listener("tls+tcp://0.0.0.0:5555", tls=cfg).start()
    """

    cdef unique_ptr[TlsConfigHandle] _handle

    cdef object __weakref__

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
        """Set the expected remote server name (client mode only).

        Used in two ways:

        * **Certificate verification**: the supplied *name* is matched against
          the identity presented in the server's certificate.
        * **SNI hint**: when Server Name Indication is used, *name* is sent
          to the server so it can select the appropriate certificate.

        .. note::
            This method is only meaningful when the configuration is used in
            client mode.
        """
        self._check()
        cdef bytes b = name.encode("utf-8")
        check_err(nng_tls_config_server_name(self._handle.get().get(), b))
        return self

    def ca_chain(self, cert_pem: str, crl_pem: str = None) -> TlsConfig:
        """Add a CA certificate chain (and optional CRL) used to validate peers.

        *cert_pem* is a PEM string containing one or more X.509 CA
        certificates; multiple certificates may be concatenated, with the
        leaf listed first.

        *crl_pem* is an optional PEM string containing a certificate
        revocation list (CRL) for the associated authority.  Pass ``None``
        to omit.

        This method may be called **multiple times** to add additional chains
        without affecting those already added.

        .. note::
            CA certificates **must** be configured when the authentication
            mode is ``TLS_AUTH_REQUIRED`` (the default for clients).
        """
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
        """Configure our own certificate and private key (PEM strings).

        *cert_pem* is the local certificate (or chain) in PEM format.  When
        providing a chain, list the leaf certificate first; the self-signed
        root may be omitted because the peer must already have it.

        *key_pem* is the matching private key in PEM format.  If the key is
        encrypted, supply the decryption *password*; pass ``None`` otherwise.

        .. warning::
            This method must only be called **once** per configuration object.
            Calling it a second time raises an error.  Use
            :meth:`cert_key_file` instead, which supports multiple calls on
            server configurations.
        """
        self._check()
        cdef bytes bc = cert_pem.encode("utf-8")
        cdef bytes bk = key_pem.encode("utf-8")
        cdef bytes bp = password.encode("utf-8") if password else None
        check_err(nng_tls_config_own_cert(
            self._handle.get().get(), bc, bk, <const char *>(bp) if bp else NULL))
        return self

    def ca_file(self, path: str) -> TlsConfig:
        """Load CA certificates (and optional CRLs) from a PEM file.

        The file at *path* must contain at least one X.509 certificate in PEM
        format and may contain multiple certificates as well as zero or more
        PEM CRL objects.  This information is used to validate peer
        certificates.

        This method may be called **multiple times** to add additional chains
        without affecting those already loaded.

        .. note::
            CA certificates **must** be configured when the authentication
            mode is ``TLS_AUTH_REQUIRED`` (the default for clients).
        """
        self._check()
        cdef bytes b = path.encode("utf-8")
        check_err(nng_tls_config_ca_file(self._handle.get().get(), b))
        return self

    def cert_key_file(self, path: str, password: str = None) -> TlsConfig:
        """Load our own certificate and private key from a PEM file.

        The file at *path* must contain both the certificate (or chain) and
        the matching private key in PEM format.  When a chain is present,
        the leaf certificate should appear first; the self-signed root may
        be omitted.

        If the private key is encrypted, supply the decryption *password*;
        pass ``None`` otherwise.

        On **server** configurations this method may be called **multiple
        times** to register certificates for different cryptographic
        algorithms (e.g. RSA and ECDSA).  On **client** configurations it
        should only be called once.
        """
        self._check()
        cdef bytes bp = path.encode("utf-8")
        cdef bytes bpw = password.encode("utf-8") if password else None
        check_err(nng_tls_config_cert_key_file(
            self._handle.get().get(), bp, <const char *>(bpw) if bpw else NULL))
        return self

    def auth_mode(self, int mode) -> TlsConfig:
        """Set the peer certificate verification mode.

        *mode* must be one of the module-level constants:

        * ``TLS_AUTH_NONE`` — no peer authentication.  Default for **server**
          configurations, where clients typically don't present certificates.
        * ``TLS_AUTH_OPTIONAL`` — verify the peer's certificate if one is
          presented; allow the session to proceed even if none is given.
        * ``TLS_AUTH_REQUIRED`` — the peer **must** present a valid certificate;
          connections without one are rejected.  Default for **client**
          configurations.
        """
        self._check()
        check_err(nng_tls_config_auth_mode(
            self._handle.get().get(), <nng_tls_auth_mode>mode))
        return self

    def version(self, int min_ver, int max_ver) -> TlsConfig:
        """Restrict the allowed TLS protocol versions.

        *min_ver* and *max_ver* must be the module-level constants
        ``TLS_VERSION_1_2`` or ``TLS_VERSION_1_3``.  Both endpoints negotiate
        the highest mutually supported version in the allowed range.

        When this method is **not called**, nng defaults to accepting both
        TLS 1.2 and TLS 1.3 (subject to what the underlying TLS engine
        supports).

        .. note::
            Only TLS 1.2 and 1.3 are supported.  SSL and earlier TLS versions
            are insecure and rejected by nng.  Some engines may not support
            restricting the *maximum* version.  0-RTT resumption is not
            supported.
        """
        self._check()
        check_err(nng_tls_config_version(
            self._handle.get().get(),
            <nng_tls_version>min_ver,
            <nng_tls_version>max_ver))
        return self

    def psk(self, identity: str, key: bytes) -> TlsConfig:
        """Configure a pre-shared key (PSK) for TLS connections.

        PSK is an alternative to certificate-based authentication.

        *identity* is a printable string that acts as a public identifier
        (similar to a user name).  Embedded NUL bytes are not supported.
        Typical implementations support identities up to 64 bytes.

        *key* is the secret bytes used to derive session keys.  Typical
        implementations support keys up to 32 bytes.

        In **client** mode this method should be called **once** to set the
        client's own identity and key.

        In **server** mode this method may be called **multiple times** to
        register keys for different clients; the server will look up the
        appropriate key when a client connects using its identity.

        .. note::
            PSK support depends on the underlying TLS engine.  Not all
            engines expose this functionality.
        """
        self._check()
        cdef bytes b_id = identity.encode("utf-8")
        cdef const unsigned char[::1] mv = key
        check_err(nng_tls_config_psk(
            self._handle.get().get(),
            b_id,
            &mv[0],
            len(mv)))
        return self

    def __repr__(self) -> str:
        cdef bint valid = self._handle.get() != NULL and self._handle.get().is_valid()
        return f"TlsConfig({'valid' if valid else 'closed'})"


# ── TLS auth-mode and version constants (re-exported at module level) ─────────
TLS_AUTH_NONE     = NNG_TLS_AUTH_MODE_NONE      #: No peer authentication (server default)
TLS_AUTH_OPTIONAL = NNG_TLS_AUTH_MODE_OPTIONAL  #: Verify cert if presented
TLS_AUTH_REQUIRED = NNG_TLS_AUTH_MODE_REQUIRED  #: Peer cert required (client default)
TLS_VERSION_1_2   = NNG_TLS_1_2                 #: TLS 1.2
TLS_VERSION_1_3   = NNG_TLS_1_3                 #: TLS 1.3

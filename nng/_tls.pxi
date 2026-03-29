# nng/_tls.pxi – included into _nng.pyx
#
# TlsCert:   owning wrapper for an X.509 certificate.
# TlsConfig: wraps nng_tls_config* via a C++ TlsConfigHandle (RAII).
#
# Lifetime: TlsConfig._handle is a shared_ptr[TlsConfigHandle] so that
# DialerHandle/ListenerHandle can co-own the config.  Python callers do not
# need to keep the TlsConfig object alive after passing it to add_dialer /
# add_listener — the C++ layer will keep the underlying nng_tls_config* alive.

import datetime as _datetime

# ── TlsCert ───────────────────────────────────────────────────────────────────

cdef class TlsCert:
    """X.509 certificate from a TLS peer or parsed from PEM/DER.
    """

    cdef nng_tls_cert *_ptr
    cdef list _alt_names_cache

    cdef object __weakref__

    def __cinit__(self) -> None:
        """Initialize with null pointer."""
        self._ptr = NULL
        self._alt_names_cache = None

    def __init__(self):
        """Raise TypeError — use the class-method factories."""
        raise TypeError("TlsCert cannot be instantiated directly. Use TlsCert.from_pem() or .from_der().")

    def __dealloc__(self) -> None:
        """Free the owned certificate."""
        if self._ptr is not NULL:
            nng_tls_cert_free(self._ptr)
            self._ptr = NULL

    @staticmethod
    cdef TlsCert _create(nng_tls_cert *ptr):
        """Wrap an owning nng_tls_cert pointer; calls nng_tls_cert_free on GC."""
        cdef TlsCert obj = TlsCert.__new__(TlsCert)
        obj._ptr = ptr
        return obj

    cdef inline void _require_valid(self) except *:
        if self._ptr is NULL:
            raise ValueError("TlsCert is not valid (null pointer)")

    # ── Factories ─────────────────────────────────────────────────────────────

    @classmethod
    def from_pem(cls, pem: str | bytes) -> TlsCert:
        """Parse a PEM-encoded X.509 certificate.

        Args:
            pem: PEM text (``str`` or ``bytes``).  When multiple certificates
                are concatenated only the first is used.

        Returns:
            A new :class:`TlsCert`.

        Raises:
            NngError: If the PEM is invalid or no TLS engine is loaded.
        """
        cdef bytes b = pem.encode("ascii") if isinstance(pem, str) else <bytes>pem
        cdef nng_tls_cert *ptr = NULL
        # mbedtls_x509_crt_parse requires size to include the null terminator
        # to recognise PEM format (checks buf[size-1] == '\0').
        # CPython bytes always have a hidden null byte at b[len(b)], so
        # passing len(b)+1 is safe.
        check_err(nng_tls_cert_parse_pem(&ptr, b, len(b) + 1))
        return TlsCert._create(ptr)

    @classmethod
    def from_der(cls, der: bytes) -> TlsCert:
        """Parse a DER-encoded X.509 certificate.

        Args:
            der: Raw DER bytes of the certificate.

        Returns:
            A new :class:`TlsCert`.

        Raises:
            NngError: If the DER is invalid or no TLS engine is loaded.
        """
        cdef nng_tls_cert *ptr = NULL
        cdef const unsigned char[::1] mv = der
        check_err(nng_tls_cert_parse_der(&ptr, &mv[0], len(mv)))
        return TlsCert._create(ptr)

    # ── Export ────────────────────────────────────────────────────────────────

    def to_der(self) -> bytes:
        """Export the certificate as DER-encoded bytes.

        Useful for interoperability with other crypto libraries (OpenSSL,
        mbedTLS, etc.).

        Returns:
            Raw DER bytes.

        Raises:
            ValueError: If the certificate pointer is null.
        """
        self._require_valid()

        # First pass: determine required buffer size
        cdef size_t size = 0
        nng_tls_cert_der(self._ptr, NULL, &size)
        if size == 0:
            return b""

        # Second pass: fill the buffer
        cdef bytearray buf = bytearray(size)
        cdef unsigned char[::1] mv = buf
        nng_tls_cert_der(self._ptr, &mv[0], &size)
        return bytes(buf[:size])

    # ── Subject / issuer ──────────────────────────────────────────────────────

    @property
    def subject(self) -> str:
        """Full subject distinguished name, e.g. ``'CN=example.com,O=Acme'``.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: If the TLS engine does not support this attribute.
        """
        self._require_valid()
        cdef char *s = NULL
        check_err(nng_tls_cert_subject(self._ptr, &s))
        return s.decode("utf-8", errors="replace")

    @property
    def issuer(self) -> str:
        """Full issuer distinguished name.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: If the TLS engine does not support this attribute.
        """
        self._require_valid()
        cdef char *s = NULL
        check_err(nng_tls_cert_issuer(self._ptr, &s))
        return s.decode("utf-8", errors="replace")

    @property
    def serial_number(self) -> str:
        """Serial number as a colon-separated hex string, e.g. ``'01:ab:cd:...'``.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: If the TLS engine does not support this attribute.
        """
        self._require_valid()
        cdef char *s = NULL
        check_err(nng_tls_cert_serial_number(self._ptr, &s))
        return s.decode("utf-8", errors="replace")

    @property
    def subject_cn(self) -> str:
        """Common name (CN) from the subject, e.g. ``'example.com'``.

        Modern certificates prefer Subject Alternative Names (SANs) over CN
        for identity.  Use :attr:`alt_names` when verifying host identity.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: If the TLS engine does not support this attribute.
        """
        self._require_valid()
        cdef char *s = NULL
        check_err(nng_tls_cert_subject_cn(self._ptr, &s))
        return s.decode("utf-8", errors="replace")

    @property
    def alt_names(self) -> list[str]:
        """Subject Alternative Names (SANs), if any.

        Returns an ordered list of SAN strings.  DNS names appear as plain
        hostnames (``'example.com'``); IP addresses as ``'IP:192.0.2.1'``.
        Returns an empty list when no SANs are present.

        .. note::
            NNG iterates SANs with a stateful internal cursor.  This property
            drains all names on the **first** call and caches the result;
            subsequent calls return the same cached list.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: On unexpected errors from the TLS engine.
        """
        if self._alt_names_cache is not None:
            return list(self._alt_names_cache)

        self._require_valid()

        result: list[str] = []
        cdef char *s = NULL
        cdef int rv

        # Drain the SAN cursor; NNG_ENOENT signals end of list
        while True:
            s = NULL
            rv = nng_tls_cert_next_alt(self._ptr, &s)
            if rv == NNG_ENOENT:
                break
            if rv != 0:
                check_err(rv)
            result.append(s.decode("utf-8", errors="replace"))

        self._alt_names_cache = result
        return list(result)

    # ── Validity window ───────────────────────────────────────────────────────

    @property
    def not_before(self) -> _datetime.datetime:
        """Certificate validity start time as a UTC-aware :class:`datetime`.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: If the TLS engine does not support this attribute.
        """
        self._require_valid()
        cdef tm t
        check_err(nng_tls_cert_not_before(self._ptr, &t))
        return _datetime.datetime(
            t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec,
            tzinfo=_datetime.timezone.utc,
        )

    @property
    def not_after(self) -> _datetime.datetime:
        """Certificate validity end time as a UTC-aware :class:`datetime`.

        Raises:
            ValueError: If the certificate pointer is null.
            NngError: If the TLS engine does not support this attribute.
        """
        self._require_valid()
        cdef tm t
        check_err(nng_tls_cert_not_after(self._ptr, &t))
        return _datetime.datetime(
            t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec,
            tzinfo=_datetime.timezone.utc,
        )

    def __repr__(self) -> str:
        if self._ptr is NULL:
            return "TlsCert(invalid)"
        try:
            return f"TlsCert(cn={self.subject_cn!r})"
        except Exception:
            return "TlsCert(...)"

    def __eq__(self, other) -> bool:
        if not isinstance(other, TlsCert):
            return NotImplemented
        if self._ptr == NULL or (<TlsCert>other)._ptr == NULL:
            return False
        # Compare DER bytes for equality; this is unambiguous and engine-agnostic.
        return self.to_der() == (<TlsCert>other).to_der()

    def __hash__(self) -> int:
        return hash(self.to_der())

    def __bool__(self) -> bool:
        return self._ptr != NULL


# ── TlsConfig ─────────────────────────────────────────────────────────────────

cdef class TlsConfig:
    """TLS configuration object for securing nng connections.

    Pass as the ``tls=`` argument to :meth:`Socket.add_dialer` or
    :meth:`Socket.add_listener`.

    Instances are created exclusively through the class-method factories:

    * :meth:`for_client` — certificate-based TLS client.
    * :meth:`for_server` — certificate-based TLS server.
    * :meth:`for_psk_client` — PSK-based TLS client.
    * :meth:`for_psk_server` — PSK-based TLS server (multiple identities).

    A single :class:`TlsConfig` may be shared across multiple dialers and
    listeners.  Once attached to a started dialer or listener the
    configuration is locked by nng and cannot be changed.

    **PSK engine compatibility**

    :meth:`for_psk_client` and :meth:`for_psk_server` are supported by all
    bundled engines, with one caveat: wolfSSL requires the
    ``NNG_SUPP_TLS_PSK`` compile-time flag; calling these factories on such a
    build raises :exc:`NngNotSupported`.

    Example (mTLS client)::

        cfg = TlsConfig.for_client(
            server_name="example.com",
            ca_pem=ca_bundle,
            cert_pem=my_cert,
            key_pem=my_key,
        )
        sock.add_dialer("tls+tcp://example.com:5555", tls=cfg).start()

    Example (TLS server)::

        cfg = TlsConfig.for_server(cert_file="/etc/ssl/server.pem")
        sock.add_listener("tls+tcp://0.0.0.0:5555", tls=cfg).start()
    """

    cdef shared_ptr[TlsConfigHandle] _handle

    cdef object __weakref__

    def __cinit__(self, *args, **kwargs):
        # _handle default-constructed (empty) by Cython's C++ member glue.
        pass

    def __init__(self, *args, **kwargs):
        """Raise TypeError — use the class-method factories."""
        raise TypeError(
            "TlsConfig cannot be instantiated directly. "
            "Use TlsConfig.for_client(), .for_server(), "
            ".for_psk_client() or .for_psk_server()."
        )

    # __dealloc__ intentionally omitted: shared_ptr[TlsConfigHandle] destructor
    # decrements the ref count; nng_tls_config_free is called only when the
    # last owner (dialer, listener, or this TlsConfig) releases the handle.

    @staticmethod
    cdef TlsConfig _alloc(bint server):
        """Allocate a new TlsConfig bypassing __init__."""
        check_nng_init()
        cdef TlsConfig obj = TlsConfig.__new__(TlsConfig)
        cdef int err = 0
        obj._handle = TlsConfigHandle.alloc(
            <nng_tls_mode>(NNG_TLS_MODE_SERVER if server else NNG_TLS_MODE_CLIENT), err)
        check_err(err)
        return obj

    cdef inline void _check(self) except *:
        if not self._handle or not self._handle.get().is_valid():
            raise NngClosed(NNG_ECLOSED, "TlsConfig is closed")

    # ── Internal accessor (used by Dialer / Listener) ─────────────────────
    cdef nng_tls_config *_get_ptr(self) except NULL:
        """Return the raw nng_tls_config pointer; raises if invalid."""
        self._check()
        return self._handle.get().get()

    # ── Factories ─────────────────────────────────────────────────────────

    @classmethod
    def for_client(
        cls,
        *,
        server_name: str | None = None,
        ca_pem: str | None = None,
        crl_pem: str | None = None,
        ca_file: str | None = None,
        cert_pem: str | None = None,
        key_pem: str | None = None,
        cert_file: str | None = None,
        password: str | None = None,
        auth_mode: int | None = None,
        min_version: int | None = None,
        max_version: int | None = None,
    ) -> TlsConfig:
        """Create a :class:`TlsConfig` for certificate-based TLS client use.

        All parameters are keyword-only and optional; supply only those
        relevant to your use case.

        Args:
            server_name: Expected server hostname for SNI and cert verification.
            ca_pem:      PEM-encoded CA certificates for peer verification.
                         Multiple certificates can be concatenated.
            crl_pem:     PEM-encoded CRL to pair with *ca_pem*.  wolfSSL
                         ignores this unless compiled with
                         ``NNG_WOLFSSL_HAVE_CRL``.
            ca_file:     Path to a PEM file with CA certs (and optional CRLs).
            cert_pem:    PEM-encoded client certificate (requires *key_pem*).
            key_pem:     PEM-encoded private key for *cert_pem*.  Encrypted
                         keys require *password*.
            cert_file:   Path to a PEM file containing both cert and key.
                         Encrypted keys require *password*.
            password:    Passphrase to decrypt *key_pem* or the key in
                         *cert_file*.
            auth_mode:   One of the ``TLS_AUTH_*`` constants.  Defaults to
                         ``TLS_AUTH_REQUIRED``.
            min_version: Minimum TLS version (``TLS_VERSION_1_2`` or
                         ``TLS_VERSION_1_3``).
            max_version: Maximum TLS version.  wolfSSL ignores this argument.

        Returns:
            A new :class:`TlsConfig` in client mode.

        Raises:
            ValueError: If *crl_pem* is set without *ca_pem*.
            NngCryptoError: If a certificate or private key cannot be parsed.
            NngNoMemory: If a memory allocation fails.
            NngNotSupported: If *min_version*/*max_version* specifies a TLS
                version unsupported by the active engine.
            NngError: If *ca_file* or *cert_file* cannot be opened
                (``NNG_ENOENT``) or read (``NNG_EACCESS``).
        """
        cdef TlsConfig cfg = TlsConfig._alloc(False)
        cdef nng_tls_config *ptr = cfg._handle.get().get()
        cdef bytes _b
        cdef bytes _bk
        cdef bytes _bp
        cdef bytes _bcrl

        # SNI / hostname verification
        if server_name is not None:
            _b = server_name.encode("utf-8")
            check_err(nng_tls_config_server_name(ptr, _b))

        # CA chain + optional CRL for peer verification
        if ca_pem is not None:
            _b = ca_pem.encode("utf-8")
            _bcrl = crl_pem.encode("utf-8") if crl_pem is not None else None
            check_err(nng_tls_config_ca_chain(
                ptr, _b, <const char *>_bcrl if _bcrl is not None else NULL))
        elif crl_pem is not None:
            raise ValueError("crl_pem requires ca_pem to be set")

        # CA chain from file
        if ca_file is not None:
            _b = ca_file.encode("utf-8")
            check_err(nng_tls_config_ca_file(ptr, _b))

        # Own certificate: PEM strings
        if cert_pem is not None and key_pem is not None:
            _b = cert_pem.encode("utf-8")
            _bk = key_pem.encode("utf-8")
            _bp = password.encode("utf-8") if password is not None else None
            check_err(nng_tls_config_own_cert(
                ptr, _b, _bk, <const char *>_bp if _bp is not None else NULL))

        # Own certificate: PEM file
        elif cert_file is not None:
            _b = cert_file.encode("utf-8")
            _bp = password.encode("utf-8") if password is not None else None
            check_err(nng_tls_config_cert_key_file(
                ptr, _b, <const char *>_bp if _bp is not None else NULL))

        # Peer authentication mode
        if auth_mode is not None:
            check_err(nng_tls_config_auth_mode(ptr, <nng_tls_auth_mode>auth_mode))

        # TLS version range
        if min_version is not None or max_version is not None:
            check_err(nng_tls_config_version(
                ptr,
                <nng_tls_version>(min_version if min_version is not None else NNG_TLS_1_2),
                <nng_tls_version>(max_version if max_version is not None else NNG_TLS_1_3)))

        return cfg

    @classmethod
    def for_server(
        cls,
        *,
        cert_pem: str | None = None,
        key_pem: str | None = None,
        cert_file: str | None = None,
        password: str | None = None,
        ca_pem: str | None = None,
        crl_pem: str | None = None,
        ca_file: str | None = None,
        auth_mode: int | None = None,
        min_version: int | None = None,
        max_version: int | None = None,
    ) -> TlsConfig:
        """Create a :class:`TlsConfig` for certificate-based TLS server use.

        All parameters are keyword-only and optional; supply only those
        relevant to your use case.

        On **mbedTLS** and **OpenSSL** a server may register multiple
        cert+key pairs (e.g. RSA + ECDSA) so the engine can select the best
        match per client.  This factory supports a single pair; for
        multi-algorithm servers assemble the config with the C API directly.
        On wolfSSL, *cert_pem* / *cert_file* replaces any previously set cert.

        Args:
            cert_pem:    PEM-encoded server certificate (requires *key_pem*).
            key_pem:     PEM-encoded private key for *cert_pem*.  Encrypted
                         keys require *password*.
            cert_file:   Path to a PEM file containing both cert and key.
            password:    Passphrase to decrypt the private key.
            ca_pem:      PEM-encoded CA certificates for client cert
                         verification (mTLS).  Required when *auth_mode* is
                         not ``TLS_AUTH_NONE``.
            crl_pem:     PEM-encoded CRL to pair with *ca_pem*.  wolfSSL
                         ignores this unless compiled with
                         ``NNG_WOLFSSL_HAVE_CRL``.
            ca_file:     Path to a PEM file with CA certs for client
                         verification.
            auth_mode:   One of the ``TLS_AUTH_*`` constants.  Defaults to
                         ``TLS_AUTH_NONE``.
            min_version: Minimum TLS version (``TLS_VERSION_1_2`` or
                         ``TLS_VERSION_1_3``).
            max_version: Maximum TLS version.  wolfSSL ignores this argument.

        Returns:
            A new :class:`TlsConfig` in server mode.

        Raises:
            ValueError: If *crl_pem* is set without *ca_pem*.
            NngCryptoError: If a certificate or private key cannot be parsed.
            NngNoMemory: If a memory allocation fails.
            NngNotSupported: If *min_version*/*max_version* specifies a TLS
                version unsupported by the active engine.
            NngError: If *ca_file* or *cert_file* cannot be opened
                (``NNG_ENOENT``) or read (``NNG_EACCESS``).
        """
        cdef TlsConfig cfg = TlsConfig._alloc(True)
        cdef nng_tls_config *ptr = cfg._handle.get().get()
        cdef bytes _b
        cdef bytes _bk
        cdef bytes _bp
        cdef bytes _bcrl

        # Own certificate: PEM strings
        if cert_pem is not None and key_pem is not None:
            _b = cert_pem.encode("utf-8")
            _bk = key_pem.encode("utf-8")
            _bp = password.encode("utf-8") if password is not None else None
            check_err(nng_tls_config_own_cert(
                ptr, _b, _bk, <const char *>_bp if _bp is not None else NULL))

        # Own certificate: PEM file
        elif cert_file is not None:
            _b = cert_file.encode("utf-8")
            _bp = password.encode("utf-8") if password is not None else None
            check_err(nng_tls_config_cert_key_file(
                ptr, _b, <const char *>_bp if _bp is not None else NULL))

        # CA chain + optional CRL for client cert verification (mTLS)
        if ca_pem is not None:
            _b = ca_pem.encode("utf-8")
            _bcrl = crl_pem.encode("utf-8") if crl_pem is not None else None
            check_err(nng_tls_config_ca_chain(
                ptr, _b, <const char *>_bcrl if _bcrl is not None else NULL))
        elif crl_pem is not None:
            raise ValueError("crl_pem requires ca_pem to be set")

        if ca_file is not None:
            _b = ca_file.encode("utf-8")
            check_err(nng_tls_config_ca_file(ptr, _b))

        # Peer authentication mode
        if auth_mode is not None:
            check_err(nng_tls_config_auth_mode(ptr, <nng_tls_auth_mode>auth_mode))

        # TLS version range
        if min_version is not None or max_version is not None:
            check_err(nng_tls_config_version(
                ptr,
                <nng_tls_version>(min_version if min_version is not None else NNG_TLS_1_2),
                <nng_tls_version>(max_version if max_version is not None else NNG_TLS_1_3)))

        return cfg

    @classmethod
    def for_psk_client(
        cls,
        identity: str,
        key: bytes,
        *,
        min_version: int | None = None,
        max_version: int | None = None,
    ) -> TlsConfig:
        """Create a :class:`TlsConfig` for PSK-based TLS client use.

        Pre-shared key (PSK) is an alternative to certificate-based
        authentication that requires no CA infrastructure.

        *identity* is a printable string (up to ~64 bytes) that acts as a
        public identifier for this client.  Embedded NUL bytes are not
        supported.

        *key* is the shared secret used to derive session keys.  mbedTLS and
        wolfSSL limit key length to ``MBEDTLS_PSK_MAX_LEN`` (typically
        32 bytes); prefer 16–32 byte keys for widest compatibility.

        Certificate verification is **disabled** automatically.  Pure PSK
        ciphersuites do not exchange certificates, so requiring one would
        cause the TLS handshake to fail.

        Args:
            identity:    PSK identity string (analogous to a username).
            key:         PSK secret bytes.
            min_version: Minimum TLS version (``TLS_VERSION_1_2`` or
                         ``TLS_VERSION_1_3``).
            max_version: Maximum TLS version.  wolfSSL ignores this argument.

        Returns:
            A new :class:`TlsConfig` in client mode.

        Raises:
            NngNotSupported: On wolfSSL when ``NNG_SUPP_TLS_PSK`` is not set.
            NngCryptoError: If the key or identity is rejected by the engine.
            NngNoMemory: If a memory allocation fails.
        """
        cdef TlsConfig cfg = TlsConfig._alloc(False)
        cdef nng_tls_config *ptr = cfg._handle.get().get()
        cdef bytes _b_id = identity.encode("utf-8")
        cdef const unsigned char[::1] _mv = key

        # PSK uses shared-secret auth; no certificate is exchanged in pure PSK
        # ciphersuites.  Disable certificate verification so that the default
        # TLS_AUTH_REQUIRED does not cause the handshake to fail when no server
        # certificate is presented.
        check_err(nng_tls_config_auth_mode(ptr, <nng_tls_auth_mode>NNG_TLS_AUTH_MODE_NONE))
        check_err(nng_tls_config_psk(ptr, _b_id, &_mv[0], len(_mv)))

        if min_version is not None or max_version is not None:
            check_err(nng_tls_config_version(
                ptr,
                <nng_tls_version>(min_version if min_version is not None else NNG_TLS_1_2),
                <nng_tls_version>(max_version if max_version is not None else NNG_TLS_1_3)))

        return cfg

    @classmethod
    def for_psk_server(
        cls,
        psks: dict[str, bytes],
        *,
        min_version: int | None = None,
        max_version: int | None = None,
    ) -> TlsConfig:
        """Create a :class:`TlsConfig` for PSK-based TLS server use.

        PSK server configurations accept multiple client identities.  *psks*
        maps each identity string to its corresponding key bytes.

        When a PSK client connects, the engine looks up the client's identity
        in *psks* and uses the matching key to complete the handshake.

        Args:
            psks:        ``{identity: key}`` mapping of PSK pairs.  Must not
                         be empty.
            min_version: Minimum TLS version (``TLS_VERSION_1_2`` or
                         ``TLS_VERSION_1_3``).
            max_version: Maximum TLS version.  wolfSSL ignores this argument.

        Returns:
            A new :class:`TlsConfig` in server mode.

        Raises:
            ValueError: If *psks* is empty.
            NngNotSupported: On wolfSSL when ``NNG_SUPP_TLS_PSK`` is not set.
            NngCryptoError: If a key or identity is rejected by the engine.
            NngNoMemory: If a memory allocation fails.
        """
        if not psks:
            raise ValueError("psks must not be empty")

        cdef TlsConfig cfg = TlsConfig._alloc(True)
        cdef nng_tls_config *ptr = cfg._handle.get().get()
        cdef bytes _b_id
        cdef const unsigned char[::1] _mv

        # Register each identity → key pair
        for identity_str, key_bytes in psks.items():
            _b_id = (<str>identity_str).encode("utf-8")
            _mv = <bytes>key_bytes
            check_err(nng_tls_config_psk(ptr, _b_id, &_mv[0], len(_mv)))

        if min_version is not None or max_version is not None:
            check_err(nng_tls_config_version(
                ptr,
                <nng_tls_version>(min_version if min_version is not None else NNG_TLS_1_2),
                <nng_tls_version>(max_version if max_version is not None else NNG_TLS_1_3)))

        return cfg

    def __repr__(self) -> str:
        """Return a debug string for this config."""
        cdef bint valid = self._handle.get() != NULL and self._handle.get().is_valid()
        return f"TlsConfig({'valid' if valid else 'closed'})"


# ── Module-level TLS engine queries ──────────────────────────────────────────

def tls_engine_name() -> str:
    """Return the short name of the currently loaded TLS engine.

    Returns ``'none'`` when no TLS engine has been registered (i.e.
    the library was built without TLS support).

    Returns:
        A short identifier string, e.g. ``'mbed'``, ``'openssl'``, or
        ``'wolf'``.
    """
    cdef const char *name = nng_tls_engine_name()
    return name.decode("utf-8") if name else "none"


def tls_engine_description() -> str:
    """Return a human-readable description of the currently loaded TLS engine.

    Returns an empty string when no TLS engine has been registered.

    Returns:
        A free-form description string, e.g. ``'mbedTLS 3.6.2'``.
    """
    cdef const char *desc = nng_tls_engine_description()
    return desc.decode("utf-8") if desc else ""


# ── TLS auth-mode and version constants (re-exported at module level) ─────────
TLS_AUTH_NONE     = NNG_TLS_AUTH_MODE_NONE      #: No peer authentication (server default)
TLS_AUTH_OPTIONAL = NNG_TLS_AUTH_MODE_OPTIONAL  #: Verify cert if presented
TLS_AUTH_REQUIRED = NNG_TLS_AUTH_MODE_REQUIRED  #: Peer cert required (client default)
TLS_VERSION_1_2   = NNG_TLS_1_2                 #: TLS 1.2
TLS_VERSION_1_3   = NNG_TLS_1_3                 #: TLS 1.3

"""tests/test_tls.py

TLS-related tests for the nng Python bindings.

Covers:
- Module-level: ``tls_engine_name()``, ``tls_engine_description()``
- TLS constants: ``TLS_AUTH_*``, ``TLS_VERSION_*``
- ``TlsConfig`` / ``TlsCert`` direct-instantiation guards
- Without TLS engine: factories raise ``NngNotSupported``
- ``TlsConfig`` factories: ``for_client``, ``for_server``, ``for_psk_client``,
  ``for_psk_server``
- ``TlsCert``: ``from_pem``, ``from_der``, round-trip, all properties
- Pipe TLS properties on non-TLS pipes (always ``False`` / ``None``)
- Full TLS connection: server-side cert verification, peer-cert accessible
- Mutual TLS (mTLS): both sides verify each other
- PSK connection: shared-secret auth, data transfer

Skip conditions
---------------
- ``requires_tls``: skipped when ``tls_engine_name() == 'none'`` (nng compiled
  without TLS support).
- ``requires_cryptography``: skipped when the ``cryptography`` package is not
  installed (needed for dynamic certificate generation).
- ``requires_psk``: skipped when the active TLS engine was compiled without PSK
  support (e.g. WolfSSL built without ``NNG_SUPP_TLS_PSK``).

Test naming convention: ``test_<unit>_<scenario>``.
"""
import datetime
import ipaddress
import time

import pytest

import nng
from nng import (
    NngError,
    NngNotSupported,
    TLS_AUTH_NONE,
    TLS_AUTH_OPTIONAL,
    TLS_AUTH_REQUIRED,
    TLS_VERSION_1_2,
    TLS_VERSION_1_3,
    TlsCert,
    TlsConfig,
)

# ── Optional dependency: cryptography ────────────────────────────────────────

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        EllipticCurvePrivateKey,
        generate_private_key,
    )

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

# ── Skip markers ──────────────────────────────────────────────────────────────

requires_tls = pytest.mark.skipif(
    nng.tls_engine_name() == "none",
    reason="No TLS engine compiled in (tls_engine_name() == 'none')",
)
requires_cryptography = pytest.mark.skipif(
    not _HAS_CRYPTOGRAPHY,
    reason="'cryptography' package not installed",
)


def _probe_psk_support() -> bool:
    """Return True if the active TLS engine supports PSK."""
    if nng.tls_engine_name() == "none":
        return False
    try:
        TlsConfig.for_psk_client("probe", b"1234567890123456")
        return True
    except NngNotSupported:
        return False


_HAS_PSK: bool = _probe_psk_support()
requires_psk = pytest.mark.skipif(
    not _HAS_PSK,
    reason="TLS engine does not support PSK (compiled without NNG_SUPP_TLS_PSK)",
)

_TIMEOUT = 5.0  # seconds for waiting on active pipes


# ── Certificate-generation helpers ───────────────────────────────────────────

def _new_ec_key() -> "EllipticCurvePrivateKey":
    """Return a fresh EC P-256 private key."""
    return generate_private_key(SECP256R1())


def _key_pem(key: "EllipticCurvePrivateKey") -> str:
    """Serialize *key* as an unencrypted PKCS#1 / SEC1 PEM string."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _cert_pem(cert: "x509.Certificate") -> str:
    """Serialize *cert* as a PEM string."""
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def _now() -> datetime.datetime:
    """Return the current time as a UTC-aware datetime."""
    return datetime.datetime.now(datetime.timezone.utc)


def _build_ca() -> "tuple[EllipticCurvePrivateKey, x509.Certificate]":
    """Build a self-signed test CA key and certificate."""
    key = _new_ec_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(_now() - datetime.timedelta(seconds=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _build_server(
    ca_key: "EllipticCurvePrivateKey",
    ca_cert: "x509.Certificate",
) -> "tuple[EllipticCurvePrivateKey, x509.Certificate]":
    """Build a server key + leaf certificate signed by *ca_cert*.

    The cert has ``CN=localhost`` and SANs for DNS:localhost and
    IP:127.0.0.1 so that ``server_name="localhost"`` validation passes.
    """
    key = _new_ec_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(_now() - datetime.timedelta(seconds=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _build_client(
    ca_key: "EllipticCurvePrivateKey",
    ca_cert: "x509.Certificate",
) -> "tuple[EllipticCurvePrivateKey, x509.Certificate]":
    """Build a client key + leaf certificate signed by *ca_cert* (for mTLS)."""
    key = _new_ec_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-client")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(3)
        .not_valid_before(_now() - datetime.timedelta(seconds=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


# ── Module-scoped PKI fixture ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pki() -> dict[str, str]:
    """Return PEM strings for a test CA, server cert/key, and client cert/key."""
    if not _HAS_CRYPTOGRAPHY:
        pytest.skip("cryptography package not installed")

    ca_key, ca_cert = _build_ca()
    srv_key, srv_cert = _build_server(ca_key, ca_cert)
    cli_key, cli_cert = _build_client(ca_key, ca_cert)

    return {
        "ca_pem": _cert_pem(ca_cert),
        "srv_cert_pem": _cert_pem(srv_cert),
        "srv_key_pem": _key_pem(srv_key),
        "cli_cert_pem": _cert_pem(cli_cert),
        "cli_key_pem": _key_pem(cli_key),
    }


# ── Helper ─────────────────────────────────────────────────────────────────────

def _wait_for_active_pipes(
    sock: nng.Socket, n: int = 1, timeout: float = _TIMEOUT
) -> list[nng.Pipe]:
    """Poll until *sock* has at least *n* ACTIVE pipes, then return them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = [p for p in sock.pipes if p.status == nng.PipeStatus.ACTIVE]
        if len(active) >= n:
            return active
        time.sleep(0.01)
    pytest.fail(f"Timed out waiting for {n} ACTIVE pipe(s) on {sock!r}")


# =============================================================================
# 1.  Module-level TLS engine queries
# =============================================================================

def test_tls_engine_name_returns_string() -> None:
    """tls_engine_name() always returns a non-empty string."""
    name = nng.tls_engine_name()
    assert isinstance(name, str) and len(name) > 0


def test_tls_engine_description_returns_string() -> None:
    """tls_engine_description() always returns a string."""
    assert isinstance(nng.tls_engine_description(), str)


@requires_tls
def test_tls_engine_name_not_none_with_engine() -> None:
    """When a TLS engine is compiled in, tls_engine_name() is not 'none'."""
    assert nng.tls_engine_name() != "none"


@requires_tls
def test_tls_engine_description_not_empty_with_engine() -> None:
    """When a TLS engine is compiled in, description is a non-empty string."""
    assert len(nng.tls_engine_description()) > 0


# =============================================================================
# 2.  TLS constants
# =============================================================================

def test_tls_auth_constants_are_ints() -> None:
    """All TLS_AUTH_* constants are integers."""
    assert isinstance(TLS_AUTH_NONE, int)
    assert isinstance(TLS_AUTH_OPTIONAL, int)
    assert isinstance(TLS_AUTH_REQUIRED, int)


def test_tls_version_constants_are_ints() -> None:
    """Both TLS_VERSION_* constants are integers."""
    assert isinstance(TLS_VERSION_1_2, int)
    assert isinstance(TLS_VERSION_1_3, int)


def test_tls_auth_constants_distinct() -> None:
    """All three TLS_AUTH_* values are distinct."""
    assert TLS_AUTH_NONE != TLS_AUTH_OPTIONAL
    assert TLS_AUTH_OPTIONAL != TLS_AUTH_REQUIRED
    assert TLS_AUTH_NONE != TLS_AUTH_REQUIRED


def test_tls_version_constants_ordered() -> None:
    """TLS_VERSION_1_2 < TLS_VERSION_1_3 (ascending enum order)."""
    assert TLS_VERSION_1_2 < TLS_VERSION_1_3


# =============================================================================
# 3.  Direct-instantiation guards
# =============================================================================

def test_tlsconfig_direct_init_raises_type_error() -> None:
    """TlsConfig() raises TypeError."""
    with pytest.raises(TypeError, match="TlsConfig"):
        TlsConfig()


def test_tlsconfig_direct_init_message_mentions_factory() -> None:
    """TypeError message mentions at least one factory name."""
    with pytest.raises(TypeError) as exc_info:
        TlsConfig()
    msg = str(exc_info.value)
    assert "for_client" in msg or "for_server" in msg


def test_tlscert_direct_init_raises_type_error() -> None:
    """TlsCert() raises TypeError."""
    with pytest.raises(TypeError, match="TlsCert"):
        TlsCert()


# =============================================================================
# 4.  Factories without TLS engine raise NngNotSupported
# =============================================================================

@pytest.mark.skipif(
    nng.tls_engine_name() != "none",
    reason="Only meaningful when no TLS engine is compiled in",
)
def test_tlsconfig_for_client_raises_without_engine() -> None:
    """for_client() raises NngNotSupported when no TLS engine is present."""
    with pytest.raises(NngNotSupported):
        TlsConfig.for_client()


@pytest.mark.skipif(
    nng.tls_engine_name() != "none",
    reason="Only meaningful when no TLS engine is compiled in",
)
def test_tlsconfig_for_server_raises_without_engine() -> None:
    """for_server() raises NngNotSupported when no TLS engine is present."""
    with pytest.raises(NngNotSupported):
        TlsConfig.for_server()


@pytest.mark.skipif(
    nng.tls_engine_name() != "none",
    reason="Only meaningful when no TLS engine is compiled in",
)
def test_tlscert_from_pem_raises_without_engine() -> None:
    """TlsCert.from_pem() raises NngError when no TLS engine is present."""
    with pytest.raises(NngError):
        TlsCert.from_pem("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")


# =============================================================================
# 5.  Pipe TLS properties on non-TLS pipes (always available)
# =============================================================================

def test_pipe_tls_verified_false_on_inproc() -> None:
    """Inproc pipes always have tls_verified=False."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tls_verified").start()
        cli.add_dialer("inproc://test_tls_verified").start()
        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.tls_verified is False


def test_pipe_tls_peer_cn_none_on_inproc() -> None:
    """Inproc pipes always have tls_peer_cn=None."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tls_peer_cn").start()
        cli.add_dialer("inproc://test_tls_peer_cn").start()
        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.tls_peer_cn is None


def test_pipe_get_peer_cert_none_on_inproc() -> None:
    """Inproc pipes always have get_peer_cert()=None."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        srv.add_listener("inproc://test_tls_peer_cert").start()
        cli.add_dialer("inproc://test_tls_peer_cert").start()
        pipe = _wait_for_active_pipes(srv)[0]
        assert pipe.get_peer_cert() is None


def test_pipe_tls_verified_false_on_plain_tcp() -> None:
    """Plain TCP pipes always have tls_verified=False."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}").start()
        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.tls_verified is False


def test_pipe_tls_peer_cn_none_on_plain_tcp() -> None:
    """Plain TCP pipes always have tls_peer_cn=None."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}").start()
        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.tls_peer_cn is None


def test_pipe_get_peer_cert_none_on_plain_tcp() -> None:
    """Plain TCP pipes always have get_peer_cert()=None."""
    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tcp://127.0.0.1:0")
        lst.start()
        cli.add_dialer(f"tcp://127.0.0.1:{lst.port}").start()
        pipe = _wait_for_active_pipes(cli)[0]
        assert pipe.get_peer_cert() is None


# =============================================================================
# 6.  TlsConfig factories (requires TLS engine)
# =============================================================================

@requires_tls
def test_for_client_minimal() -> None:
    """for_client() with no arguments returns a TlsConfig."""
    cfg = TlsConfig.for_client()
    assert isinstance(cfg, TlsConfig)


@requires_tls
def test_for_server_minimal() -> None:
    """for_server() with no arguments returns a TlsConfig."""
    cfg = TlsConfig.for_server()
    assert isinstance(cfg, TlsConfig)


@requires_tls
def test_for_client_repr_contains_valid() -> None:
    """repr of a freshly created TlsConfig contains 'valid'."""
    assert "valid" in repr(TlsConfig.for_client())


@requires_tls
def test_for_server_repr_contains_valid() -> None:
    """repr of a freshly created server TlsConfig contains 'valid'."""
    assert "valid" in repr(TlsConfig.for_server())


@requires_tls
@requires_cryptography
def test_for_client_with_ca_pem(pki: dict) -> None:
    """for_client(ca_pem=...) does not raise."""
    cfg = TlsConfig.for_client(ca_pem=pki["ca_pem"])
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_cryptography
def test_for_client_with_server_name_and_ca(pki: dict) -> None:
    """for_client(server_name=..., ca_pem=...) does not raise."""
    cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_cryptography
def test_for_client_with_client_cert(pki: dict) -> None:
    """for_client with cert_pem + key_pem does not raise."""
    cfg = TlsConfig.for_client(
        ca_pem=pki["ca_pem"],
        cert_pem=pki["cli_cert_pem"],
        key_pem=pki["cli_key_pem"],
    )
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_cryptography
def test_for_server_with_cert_and_key(pki: dict) -> None:
    """for_server(cert_pem=..., key_pem=...) does not raise."""
    cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"],
        key_pem=pki["srv_key_pem"],
    )
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_cryptography
def test_for_server_with_mtls_params(pki: dict) -> None:
    """for_server with CA + auth_mode=TLS_AUTH_REQUIRED does not raise."""
    cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"],
        key_pem=pki["srv_key_pem"],
        ca_pem=pki["ca_pem"],
        auth_mode=TLS_AUTH_REQUIRED,
    )
    assert isinstance(cfg, TlsConfig)


@requires_tls
def test_for_client_crl_without_ca_raises() -> None:
    """for_client(crl_pem=...) without ca_pem raises ValueError."""
    with pytest.raises(ValueError, match="ca_pem"):
        TlsConfig.for_client(crl_pem="---CRL---")


@requires_tls
def test_for_server_crl_without_ca_raises() -> None:
    """for_server(crl_pem=...) without ca_pem raises ValueError."""
    with pytest.raises(ValueError, match="ca_pem"):
        TlsConfig.for_server(crl_pem="---CRL---")


@requires_tls
def test_for_client_with_version_range() -> None:
    """for_client with min_version/max_version does not raise."""
    cfg = TlsConfig.for_client(min_version=TLS_VERSION_1_2, max_version=TLS_VERSION_1_3)
    assert isinstance(cfg, TlsConfig)


@requires_tls
def test_for_server_with_version_range() -> None:
    """for_server with min_version/max_version does not raise."""
    cfg = TlsConfig.for_server(min_version=TLS_VERSION_1_2, max_version=TLS_VERSION_1_3)
    assert isinstance(cfg, TlsConfig)


@requires_tls
def test_for_client_auth_mode_none() -> None:
    """for_client(auth_mode=TLS_AUTH_NONE) does not raise."""
    cfg = TlsConfig.for_client(auth_mode=TLS_AUTH_NONE)
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_psk
def test_for_psk_client_basic() -> None:
    """for_psk_client(identity, key) returns a TlsConfig."""
    cfg = TlsConfig.for_psk_client("test-client", b"1234567890123456")
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_psk
def test_for_psk_server_basic() -> None:
    """for_psk_server({'id': key}) returns a TlsConfig."""
    cfg = TlsConfig.for_psk_server({"test-client": b"1234567890123456"})
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_psk
def test_for_psk_server_multiple_identities() -> None:
    """for_psk_server with multiple identities does not raise."""
    cfg = TlsConfig.for_psk_server({
        "client-a": b"1234567890123456",
        "client-b": b"abcdefghijklmnop",
    })
    assert isinstance(cfg, TlsConfig)


@requires_tls
def test_for_psk_server_empty_raises() -> None:
    """for_psk_server({}) raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        TlsConfig.for_psk_server({})


@requires_tls
@requires_psk
def test_for_psk_client_with_version() -> None:
    """for_psk_client with version params does not raise."""
    cfg = TlsConfig.for_psk_client(
        "id", b"key", min_version=TLS_VERSION_1_2, max_version=TLS_VERSION_1_2,
    )
    assert isinstance(cfg, TlsConfig)


@requires_tls
@requires_psk
def test_for_psk_server_with_version() -> None:
    """for_psk_server with version params does not raise."""
    cfg = TlsConfig.for_psk_server(
        {"id": b"key"},
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )
    assert isinstance(cfg, TlsConfig)


# =============================================================================
# 7.  TlsCert: parsing, properties, equality, hashing
# =============================================================================

@requires_tls
@requires_cryptography
def test_tlscert_from_pem_str(pki: dict) -> None:
    """TlsCert.from_pem() accepts a str PEM and returns a TlsCert."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert isinstance(cert, TlsCert)


@requires_tls
@requires_cryptography
def test_tlscert_from_pem_bytes(pki: dict) -> None:
    """TlsCert.from_pem() accepts bytes PEM."""
    cert = TlsCert.from_pem(pki["ca_pem"].encode("ascii"))
    assert isinstance(cert, TlsCert)


@requires_tls
@requires_cryptography
def test_tlscert_to_der_non_empty(pki: dict) -> None:
    """to_der() returns a non-empty bytes object."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    der = cert.to_der()
    assert isinstance(der, bytes) and len(der) > 0


@requires_tls
@requires_cryptography
def test_tlscert_from_der_roundtrip(pki: dict) -> None:
    """from_pem() → to_der() → from_der() yields an equal cert."""
    ca_cert = TlsCert.from_pem(pki["ca_pem"])
    der = ca_cert.to_der()
    ca_cert2 = TlsCert.from_der(der)
    assert ca_cert == ca_cert2


@requires_tls
@requires_cryptography
def test_tlscert_equality_same_pem(pki: dict) -> None:
    """Two TlsCert instances parsed from the same PEM are equal."""
    a = TlsCert.from_pem(pki["ca_pem"])
    b = TlsCert.from_pem(pki["ca_pem"])
    assert a == b


@requires_tls
@requires_cryptography
def test_tlscert_inequality_different_certs(pki: dict) -> None:
    """TlsCert instances from different certs are not equal."""
    ca_cert = TlsCert.from_pem(pki["ca_pem"])
    srv_cert = TlsCert.from_pem(pki["srv_cert_pem"])
    assert ca_cert != srv_cert


@requires_tls
@requires_cryptography
def test_tlscert_not_equal_to_non_tlscert(pki: dict) -> None:
    """TlsCert.__eq__ returns NotImplemented for non-TlsCert."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert cert != "not a cert"
    assert cert != 42


@requires_tls
@requires_cryptography
def test_tlscert_hashable(pki: dict) -> None:
    """TlsCert is hashable and can be used as a dict key and set member."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    mapping = {cert: "ca"}
    assert mapping[cert] == "ca"
    assert cert in {cert}


@requires_tls
@requires_cryptography
def test_tlscert_equal_certs_same_hash(pki: dict) -> None:
    """Equal TlsCert objects have the same hash."""
    a = TlsCert.from_pem(pki["ca_pem"])
    b = TlsCert.from_pem(pki["ca_pem"])
    assert hash(a) == hash(b)


@requires_tls
@requires_cryptography
def test_tlscert_bool_true_for_valid(pki: dict) -> None:
    """A valid TlsCert is truthy."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert bool(cert) is True


@requires_tls
@requires_cryptography
def test_tlscert_repr_shows_cn(pki: dict) -> None:
    """TlsCert repr contains the subject CN."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert "Test CA" in repr(cert)


@requires_tls
@requires_cryptography
def test_tlscert_subject_cn_ca(pki: dict) -> None:
    """subject_cn returns 'Test CA' for the CA cert."""
    assert TlsCert.from_pem(pki["ca_pem"]).subject_cn == "Test CA"


@requires_tls
@requires_cryptography
def test_tlscert_subject_cn_server(pki: dict) -> None:
    """subject_cn returns 'localhost' for the server cert."""
    assert TlsCert.from_pem(pki["srv_cert_pem"]).subject_cn == "localhost"


@requires_tls
@requires_cryptography
def test_tlscert_subject_contains_cn(pki: dict) -> None:
    """subject property contains the CN string."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert isinstance(cert.subject, str) and "Test CA" in cert.subject


@requires_tls
@requires_cryptography
def test_tlscert_issuer_of_self_signed_equals_subject(pki: dict) -> None:
    """Issuer of the self-signed CA cert contains the same CN as the subject."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert "Test CA" in cert.issuer


@requires_tls
@requires_cryptography
def test_tlscert_leaf_cert_issuer_differs_from_subject(pki: dict) -> None:
    """Server cert: subject CN is 'localhost', issuer CN is 'Test CA'."""
    cert = TlsCert.from_pem(pki["srv_cert_pem"])
    assert cert.subject_cn == "localhost"
    assert "Test CA" in cert.issuer


@requires_tls
@requires_cryptography
def test_tlscert_serial_number_is_nonempty_string(pki: dict) -> None:
    """serial_number returns a non-empty string."""
    sn = TlsCert.from_pem(pki["ca_pem"]).serial_number
    assert isinstance(sn, str) and len(sn) > 0


@requires_tls
@requires_cryptography
def test_tlscert_not_before_is_utc_datetime(pki: dict) -> None:
    """not_before returns a UTC-aware datetime."""
    nb = TlsCert.from_pem(pki["ca_pem"]).not_before
    assert isinstance(nb, datetime.datetime) and nb.tzinfo is not None


@requires_tls
@requires_cryptography
def test_tlscert_not_after_is_utc_datetime(pki: dict) -> None:
    """not_after returns a UTC-aware datetime."""
    na = TlsCert.from_pem(pki["ca_pem"]).not_after
    assert isinstance(na, datetime.datetime) and na.tzinfo is not None


@requires_tls
@requires_cryptography
def test_tlscert_validity_window_ordered(pki: dict) -> None:
    """not_before < not_after for a valid cert."""
    cert = TlsCert.from_pem(pki["ca_pem"])
    assert cert.not_before < cert.not_after


@requires_tls
@requires_cryptography
def test_tlscert_alt_names_server_cert_has_localhost(pki: dict) -> None:
    """Server cert alt_names includes 'localhost'."""
    names = TlsCert.from_pem(pki["srv_cert_pem"]).alt_names
    assert isinstance(names, list)
    assert "localhost" in names


@requires_tls
@requires_cryptography
def test_tlscert_alt_names_server_cert_has_ip(pki: dict) -> None:
    """Server cert alt_names contains the IP SAN entry for 127.0.0.1."""
    names = TlsCert.from_pem(pki["srv_cert_pem"]).alt_names
    # NNG formats IP SANs as "IP:x.x.x.x" or just the address depending on engine.
    assert any("127.0.0.1" in n for n in names)


@requires_tls
@requires_cryptography
def test_tlscert_alt_names_cached(pki: dict) -> None:
    """Calling alt_names twice on the same TlsCert returns the same list."""
    cert = TlsCert.from_pem(pki["srv_cert_pem"])
    assert cert.alt_names == cert.alt_names


@requires_tls
@requires_cryptography
def test_tlscert_alt_names_empty_for_ca(pki: dict) -> None:
    """CA cert has no SANs; alt_names returns an empty list."""
    assert TlsCert.from_pem(pki["ca_pem"]).alt_names == []


# =============================================================================
# 8.  Full TLS connection (cert-based, server auth only)
# =============================================================================

@requires_tls
@requires_cryptography
def test_tls_connection_client_pipe_tls_verified(pki: dict) -> None:
    """Client-side pipe has tls_verified=True after TLS handshake."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        cli_pipe = _wait_for_active_pipes(cli)[0]
        assert cli_pipe.tls_verified is True


@requires_tls
@requires_cryptography
def test_tls_connection_client_pipe_tls_peer_cn(pki: dict) -> None:
    """Client-side pipe tls_peer_cn matches the server cert CN."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        cli_pipe = _wait_for_active_pipes(cli)[0]
        assert cli_pipe.tls_peer_cn == "localhost"


@requires_tls
@requires_cryptography
def test_tls_connection_client_pipe_get_peer_cert(pki: dict) -> None:
    """Client-side pipe get_peer_cert() returns the server certificate."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        cli_pipe = _wait_for_active_pipes(cli)[0]
        cert = cli_pipe.get_peer_cert()
        assert isinstance(cert, TlsCert)
        assert cert.subject_cn == "localhost"


@requires_tls
@requires_cryptography
def test_tls_connection_server_pipe_not_verified_without_client_cert(pki: dict) -> None:
    """Server-side pipe tls_verified=False when no client cert was sent."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        assert srv_pipe.tls_verified is False


@requires_tls
@requires_cryptography
def test_tls_connection_server_pipe_no_peer_cert_without_mtls(pki: dict) -> None:
    """Server-side pipe get_peer_cert()=None when client sent no cert."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        assert srv_pipe.get_peer_cert() is None


@requires_tls
@requires_cryptography
def test_tls_connection_data_transfer(pki: dict) -> None:
    """Data can be sent and received over a TLS-secured connection."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    with nng.RepSocket() as srv, nng.ReqSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = int(_TIMEOUT * 1000)
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()
        _wait_for_active_pipes(cli)

        cli.send(b"hello-tls")
        assert srv.recv() == b"hello-tls"
        srv.send(b"world-tls")
        assert cli.recv() == b"world-tls"


@requires_tls
@requires_cryptography
def test_tls_config_can_be_shared_across_dialers(pki: dict) -> None:
    """A single TlsConfig can be reused for multiple connections."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"], key_pem=pki["srv_key_pem"],
    )
    cli_cfg = TlsConfig.for_client(server_name="localhost", ca_pem=pki["ca_pem"])

    # Two independent listeners and dialers sharing the same configs.
    with (
        nng.PairSocket() as srv1,
        nng.PairSocket() as srv2,
        nng.PairSocket() as cli1,
        nng.PairSocket() as cli2,
    ):
        lst1 = srv1.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst1.start()
        lst2 = srv2.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst2.start()
        cli1.add_dialer(f"tls+tcp://127.0.0.1:{lst1.port}", tls=cli_cfg).start()
        cli2.add_dialer(f"tls+tcp://127.0.0.1:{lst2.port}", tls=cli_cfg).start()

        assert len(_wait_for_active_pipes(cli1)) == 1
        assert len(_wait_for_active_pipes(cli2)) == 1


# =============================================================================
# 9.  Mutual TLS (mTLS)
# =============================================================================

@requires_tls
@requires_cryptography
def test_mtls_server_pipe_tls_verified(pki: dict) -> None:
    """With mTLS, server-side pipe tls_verified=True."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"],
        key_pem=pki["srv_key_pem"],
        ca_pem=pki["ca_pem"],
        auth_mode=TLS_AUTH_REQUIRED,
    )
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=pki["ca_pem"],
        cert_pem=pki["cli_cert_pem"],
        key_pem=pki["cli_key_pem"],
    )

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        assert srv_pipe.tls_verified is True


@requires_tls
@requires_cryptography
def test_mtls_server_pipe_tls_peer_cn(pki: dict) -> None:
    """With mTLS, server-side pipe tls_peer_cn is the client cert CN."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"],
        key_pem=pki["srv_key_pem"],
        ca_pem=pki["ca_pem"],
        auth_mode=TLS_AUTH_REQUIRED,
    )
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=pki["ca_pem"],
        cert_pem=pki["cli_cert_pem"],
        key_pem=pki["cli_key_pem"],
    )

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        assert srv_pipe.tls_peer_cn == "test-client"


@requires_tls
@requires_cryptography
def test_mtls_server_pipe_get_peer_cert(pki: dict) -> None:
    """With mTLS, server-side pipe get_peer_cert() returns the client cert."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"],
        key_pem=pki["srv_key_pem"],
        ca_pem=pki["ca_pem"],
        auth_mode=TLS_AUTH_REQUIRED,
    )
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=pki["ca_pem"],
        cert_pem=pki["cli_cert_pem"],
        key_pem=pki["cli_key_pem"],
    )

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()

        srv_pipe = _wait_for_active_pipes(srv)[0]
        cert = srv_pipe.get_peer_cert()
        assert isinstance(cert, TlsCert)
        assert cert.subject_cn == "test-client"


@requires_tls
@requires_cryptography
def test_mtls_data_transfer(pki: dict) -> None:
    """Data can be sent and received over a mutual-TLS connection."""
    srv_cfg = TlsConfig.for_server(
        cert_pem=pki["srv_cert_pem"],
        key_pem=pki["srv_key_pem"],
        ca_pem=pki["ca_pem"],
        auth_mode=TLS_AUTH_REQUIRED,
    )
    cli_cfg = TlsConfig.for_client(
        server_name="localhost",
        ca_pem=pki["ca_pem"],
        cert_pem=pki["cli_cert_pem"],
        key_pem=pki["cli_key_pem"],
    )

    with nng.RepSocket() as srv, nng.ReqSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = int(_TIMEOUT * 1000)
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()
        _wait_for_active_pipes(cli)

        cli.send(b"mtls-ping")
        assert srv.recv() == b"mtls-ping"
        srv.send(b"mtls-pong")
        assert cli.recv() == b"mtls-pong"


# =============================================================================
# 10.  PSK connection
#
# PSK ciphersuites (TLS 1.2) do not exchange X.509 certificates; auth is
# done entirely via the shared key.  Version is capped at TLS 1.2 because
# traditional PSK ciphersuites are TLS 1.2 only (TLS 1.3 uses PSK only for
# session resumption, which is a different code path).
# =============================================================================

_PSK_IDENTITY = "test-client"
_PSK_KEY = b"test_key_16bytes"  # 16-byte PSK (128-bit)


@requires_tls
@requires_psk
def test_psk_connection_data_transfer() -> None:
    """PSK client + server can exchange messages."""
    srv_cfg = TlsConfig.for_psk_server(
        {_PSK_IDENTITY: _PSK_KEY},
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )
    cli_cfg = TlsConfig.for_psk_client(
        _PSK_IDENTITY, _PSK_KEY,
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )

    with nng.RepSocket() as srv, nng.ReqSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = int(_TIMEOUT * 1000)
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()
        _wait_for_active_pipes(cli)

        cli.send(b"psk-ping")
        assert srv.recv() == b"psk-ping"
        srv.send(b"psk-pong")
        assert cli.recv() == b"psk-pong"


@requires_tls
@requires_psk
def test_psk_connection_pipe_no_peer_cert() -> None:
    """PSK connection: get_peer_cert() returns None (no certificate)."""
    srv_cfg = TlsConfig.for_psk_server(
        {_PSK_IDENTITY: _PSK_KEY},
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )
    cli_cfg = TlsConfig.for_psk_client(
        _PSK_IDENTITY, _PSK_KEY,
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )

    with nng.PairSocket() as srv, nng.PairSocket() as cli:
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()
        cli_pipe = _wait_for_active_pipes(cli)[0]

        # No X.509 certificate is exchanged in PSK mode.
        assert cli_pipe.get_peer_cert() is None


@requires_tls
@requires_psk
def test_psk_multikey_server_accepts_correct_identity() -> None:
    """PSK server with multiple identities accepts the correct one."""
    srv_cfg = TlsConfig.for_psk_server(
        {
            "other-client": b"other_key_16byte",
            _PSK_IDENTITY: _PSK_KEY,
        },
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )
    cli_cfg = TlsConfig.for_psk_client(
        _PSK_IDENTITY, _PSK_KEY,
        min_version=TLS_VERSION_1_2,
        max_version=TLS_VERSION_1_2,
    )

    with nng.RepSocket() as srv, nng.ReqSocket() as cli:
        srv.recv_timeout = cli.recv_timeout = int(_TIMEOUT * 1000)
        lst = srv.add_listener("tls+tcp://127.0.0.1:0", tls=srv_cfg)
        lst.start()
        cli.add_dialer(f"tls+tcp://127.0.0.1:{lst.port}", tls=cli_cfg).start()
        _wait_for_active_pipes(cli)

        cli.send(b"multi-psk")
        assert srv.recv() == b"multi-psk"
        srv.send(b"ok")
        assert cli.recv() == b"ok"

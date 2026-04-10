"""Ephemeral PKI builder for the TLS demo scripts.

Generates an in-memory, single-use CA plus one server leaf certificate and one
client leaf certificate, all signed with EC P-256 keys.  The certificates are
returned as PEM strings so they can be passed directly to
:meth:`nng.TlsConfig.for_server` and :meth:`nng.TlsConfig.for_client`.

This module is intentionally a private helper (leading underscore) — it is only
imported by the sibling demo scripts and is not meant for production use.

Requirements: ``cryptography>=42.0``  (``pip install cryptography``)
"""

import datetime
import ipaddress
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1,
    EllipticCurvePrivateKey,
    generate_private_key,
)
from cryptography.x509.oid import NameOID


@dataclass(frozen=True, kw_only=True)
class Pki:
    """Immutable bundle of PEM strings for a single-CA PKI.

    All fields are UTF-8 PEM strings ready to be passed directly to
    :class:`nng.TlsConfig` factories.

    Attributes:
        ca_pem: PEM-encoded self-signed CA certificate.
        srv_cert_pem: PEM-encoded server leaf certificate (CN=localhost,
            SAN: DNS:localhost + IP:127.0.0.1).
        srv_key_pem: Unencrypted PEM-encoded EC private key for *srv_cert_pem*.
        cli_cert_pem: PEM-encoded client leaf certificate (CN=demo-client).
        cli_key_pem: Unencrypted PEM-encoded EC private key for *cli_cert_pem*.
    """

    ca_pem: str
    srv_cert_pem: str
    srv_key_pem: str
    cli_cert_pem: str
    cli_key_pem: str


def build_pki() -> Pki:
    """Build and return an ephemeral single-CA PKI for demo purposes.

    Creates:

    * A self-signed EC P-256 CA certificate valid for 24 hours.
    * A server leaf certificate with ``CN=localhost`` and a Subject
      Alternative Name covering ``DNS:localhost`` and ``IP:127.0.0.1``,
      valid for 24 hours.
    * A client leaf certificate with ``CN=demo-client``, valid for
      24 hours.

    All keys are EC P-256; all signatures use SHA-256.

    Returns:
        A :class:`Pki` dataclass with five PEM fields.
    """

    def _new_key() -> EllipticCurvePrivateKey:
        """Return a fresh EC P-256 private key."""
        return generate_private_key(SECP256R1())

    def _now() -> datetime.datetime:
        """Return the current UTC-aware datetime."""
        return datetime.datetime.now(datetime.timezone.utc)

    def _key_pem(key: EllipticCurvePrivateKey) -> str:
        """Serialize *key* as an unencrypted PKCS-1/TraditionalOpenSSL PEM string."""
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()

    def _cert_pem(cert: x509.Certificate) -> str:
        """Serialize *cert* as a PEM string."""
        return cert.public_bytes(serialization.Encoding.PEM).decode()

    # Build CA
    ca_key = _new_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Demo CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(_now() - datetime.timedelta(seconds=5))
        .not_valid_after(_now() + datetime.timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # Build server leaf (SAN covers DNS:localhost and IP:127.0.0.1)
    srv_key = _new_key()
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(2)
        .not_valid_before(_now() - datetime.timedelta(seconds=5))
        .not_valid_after(_now() + datetime.timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Build client leaf
    cli_key = _new_key()
    cli_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "demo-client")]))
        .issuer_name(ca_cert.subject)
        .public_key(cli_key.public_key())
        .serial_number(3)
        .not_valid_before(_now() - datetime.timedelta(seconds=5))
        .not_valid_after(_now() + datetime.timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    return Pki(
        ca_pem=_cert_pem(ca_cert),
        srv_cert_pem=_cert_pem(srv_cert),
        srv_key_pem=_key_pem(srv_key),
        cli_cert_pem=_cert_pem(cli_cert),
        cli_key_pem=_key_pem(cli_key),
    )

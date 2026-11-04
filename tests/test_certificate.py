# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The certificate users download is the CA, not the server certificate (#74)."""

import datetime
import ipaddress
import socket
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from orbitalwindow.api import served_ca


def make_cert(name, key, issuer, issuer_key, ca):
    """Make a certificate like make-cert.sh does.

    Args:
        name: Common name.
        key: The certificate's private key.
        issuer: The issuer's name, or None for a self-signed certificate.
        issuer_key: The issuer's private key.
        ca: Whether the certificate is a CA.

    Returns:
        The certificate.
    """
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer or subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def serve_once(tmp_path, chain, key):
    """Serve one TLS handshake with the given chain on a free local port.

    Args:
        tmp_path: Folder for the PEM files.
        chain: Certificates, server certificate first.
        key: The server certificate's private key.

    Returns:
        The address the server listens on.
    """
    pem = tmp_path / "chain.pem"
    pem.write_bytes(b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain))
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem, key_file)
    listener = socket.create_server(("127.0.0.1", 0))

    def answer():
        """Accept one connection and finish its handshake."""
        conn, _ = listener.accept()
        with context.wrap_socket(conn, server_side=True):
            pass
        listener.close()

    threading.Thread(target=answer, daemon=True).start()
    return listener.getsockname()


def test_served_ca_is_the_top_of_the_chain(tmp_path):
    """With a CA and a server certificate, the CA is served."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    ca = make_cert("OrbitalWindow CA", ca_key, None, ca_key, ca=True)
    server = make_cert("127.0.0.1", server_key, ca.subject, ca_key, ca=False)
    pem = served_ca(serve_once(tmp_path, [server, ca], server_key), timeout=5)
    assert x509.load_pem_x509_certificate(pem.encode()) == ca


def test_served_ca_of_an_old_self_signed_certificate(tmp_path):
    """An older single self-signed certificate is served as it is."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = make_cert("127.0.0.1", key, None, key, ca=True)
    pem = served_ca(serve_once(tmp_path, [cert], key), timeout=5)
    assert x509.load_pem_x509_certificate(pem.encode()) == cert

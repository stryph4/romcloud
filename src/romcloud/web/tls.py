"""TLS certificate support for secure-context browser APIs."""

from __future__ import annotations

import ipaddress
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from romcloud.infrastructure.atomic_file import atomic_write_text


def ensure_manager_certificate(data_path: str | Path) -> tuple[Path, Path]:
    """Return a stable self-signed certificate/key, creating them if absent."""
    directory = Path(data_path) / "web"
    certificate_path = directory / "manager-cert.pem"
    key_path = directory / "manager-key.pem"
    if certificate_path.is_file() and key_path.is_file():
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return certificate_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname() or "batocera"
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ROMCloud"),
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )
    now = datetime.now(timezone.utc)
    names: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName("batocera"),
        x509.DNSName(hostname),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except OSError:
        addresses = []
    for address in addresses:
        try:
            value = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        except ValueError:
            continue
        candidate = x509.IPAddress(value)
        if candidate not in names:
            names.append(candidate)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    atomic_write_text(key_path, key_pem, mode=0o600)
    atomic_write_text(certificate_path, certificate_pem, mode=0o644)
    return certificate_path, key_path

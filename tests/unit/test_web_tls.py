from __future__ import annotations

import ssl

from cryptography import x509
from cryptography.x509.oid import ExtensionOID

from romcloud.web.tls import ensure_manager_certificate


def test_generated_manager_certificate_is_stable_and_loadable(tmp_path) -> None:
    certificate, key = ensure_manager_certificate(tmp_path)
    first_certificate = certificate.read_bytes()
    first_key = key.read_bytes()

    same_certificate, same_key = ensure_manager_certificate(tmp_path)

    assert same_certificate == certificate and same_key == key
    assert same_certificate.read_bytes() == first_certificate
    assert same_key.read_bytes() == first_key
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    parsed = x509.load_pem_x509_certificate(first_certificate)
    names = parsed.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert "127.0.0.1" in {
        str(value) for value in names.get_values_for_type(x509.IPAddress)
    }

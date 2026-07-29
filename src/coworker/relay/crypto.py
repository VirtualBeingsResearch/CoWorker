"""Cryptographic identities used by the end-to-end encrypted Relay transport."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

TOKEN_PREFIX = "cwct_v1_"
_DOMAIN = b"coworker-relay-e2ee-v1"


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def generate_communication_token() -> str:
    return TOKEN_PREFIX + b64encode(secrets.token_bytes(32))


def is_relay_safe_token(token: str) -> bool:
    if not token.startswith(TOKEN_PREFIX):
        return False
    try:
        return len(b64decode(token.removeprefix(TOKEN_PREFIX))) == 32
    except (ValueError, TypeError):
        return False


def _seed(token: str, instance_id: str, purpose: bytes) -> bytes:
    if not is_relay_safe_token(token):
        raise ValueError("Relay requires a Coworker-generated high-entropy communication token")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(_DOMAIN + b"\x00" + instance_id.encode()).digest(),
        info=_DOMAIN + b"\x00" + purpose,
    ).derive(token.encode("utf-8"))


def private_key(token: str, instance_id: str, purpose: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(
        _seed(token, instance_id, purpose.encode("ascii"))
    )


def public_key_text(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return b64encode(
        public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def load_public_key(value: str) -> Ed25519PublicKey:
    raw = b64decode(value)
    if len(raw) != 32:
        raise ValueError("invalid Ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(raw)


def sign_text(key: Ed25519PrivateKey, payload: str) -> str:
    return b64encode(key.sign(payload.encode("utf-8")))


def verify_text(key: Ed25519PublicKey, payload: str, signature: str) -> None:
    key.verify(b64decode(signature), payload.encode("utf-8"))


def derive_server_certificate(token: str, instance_id: str) -> tuple[bytes, bytes, str]:
    """Return a stable self-signed certificate, PKCS8 key, and raw public-key pin."""

    key = private_key(token, instance_id, "inner-tls-server")
    public_text = public_key_text(key)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "coworker-relay-inner-v1")]
    )
    serial = int.from_bytes(
        hashlib.sha256(b"coworker-relay-cert-v1\x00" + b64decode(public_text)).digest()[:20],
        "big",
    ) >> 1
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(datetime(2024, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2054, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, algorithm=None)
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        public_text,
    )


def challenge_payload(
    kind: str,
    instance_id: str,
    connection_id: str,
    nonce: str,
    epoch: int,
    expires_at: int,
) -> str:
    return "\n".join(
        (
            "coworker-relay-v1",
            kind,
            instance_id,
            connection_id,
            nonce,
            str(epoch),
            str(expires_at),
        )
    )


def key_sync_payload(
    instance_id: str,
    connection_id: str,
    public_key: str,
    epoch: int,
) -> str:
    return "\n".join(
        (
            "coworker-relay-v1",
            "auth-key",
            instance_id,
            connection_id,
            str(epoch),
            public_key,
        )
    )


def session_payload(
    kind: str,
    instance_id: str,
    connection_id: str,
    session_id: str,
    source_ip: str,
    origin: str,
) -> str:
    return "\n".join(
        (
            "coworker-relay-v1",
            kind,
            instance_id,
            connection_id,
            session_id,
            source_ip,
            origin,
        )
    )

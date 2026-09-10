"""Inbound media helpers for the Weixin ClawBot CDN.

The crypto and key-encoding rules mirror Tencent's official
``@tencent-weixin/openclaw-weixin`` package: CDN payloads are AES-128-ECB
encrypted with PKCS7 padding, and a media item carries its key either as
``image_item.aeskey`` (raw 32-char hex) or ``*.media.aes_key`` (base64 of
16 raw bytes, or base64 of a 32-char hex string).
"""

from __future__ import annotations

import base64
import binascii
import io
import mimetypes
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, UnidentifiedImageError

from coworker.channels.filenames import safe_attachment_filename

WEIXIN_MEDIA_MAX_BYTES = 100 * 1024 * 1024
_DECRYPT_CHUNK_BYTES = 1024 * 1024

_EXTENSION_TO_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}


class WeixinMediaError(ValueError):
    """A media item could not be decoded or decrypted."""


class WeixinMediaTooLargeError(WeixinMediaError):
    """A media payload exceeded the accepted size limit."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"CDN media size {size} bytes exceeds limit {limit} bytes")


def resolve_media_key(aeskey_hex: str | None, media_aes_key: str | None) -> bytes | None:
    """Return the raw 16-byte AES key for a media item, or ``None`` for plain CDN data.

    ``aeskey_hex`` is ``image_item.aeskey`` and wins over ``media_aes_key``
    (``*.media.aes_key``), matching the official package.
    """
    if aeskey_hex:
        try:
            key = bytes.fromhex(aeskey_hex)
        except ValueError as error:
            raise WeixinMediaError(f"image aeskey is not a hex string: {error}") from error
        if len(key) != 16:
            raise WeixinMediaError(f"image aeskey must decode to 16 bytes, got {len(key)}")
        return key
    if media_aes_key:
        return _parse_media_aes_key(media_aes_key)
    return None


def _parse_media_aes_key(aes_key: str) -> bytes:
    try:
        decoded = base64.b64decode(aes_key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise WeixinMediaError(f"media aes_key is not valid base64: {error}") from error
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and _is_hex(decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise WeixinMediaError(
        f"media aes_key must decode to 16 raw bytes or a 32-char hex string, "
        f"got {len(decoded)} bytes"
    )


def _is_hex(value: bytes) -> bool:
    try:
        bytes.fromhex(value.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB bytes and strip PKCS7 padding."""
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise WeixinMediaError(
            f"ciphertext must be a non-empty multiple of 16 bytes, got {len(ciphertext)}"
        )
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(decryptor.update(ciphertext) + decryptor.finalize()) + (
            unpadder.finalize()
        )
    except ValueError as error:
        raise WeixinMediaError(f"PKCS7 unpadding failed: {error}") from error


def decrypt_aes_ecb_file(source: Path, destination: Path, key: bytes) -> int:
    """Stream-decrypt an AES-128-ECB file to ``destination``; return its plaintext size.

    Memory stays bounded by the chunk size regardless of file size.
    """
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        with source.open("rb") as src, destination.open("wb") as dst:
            while chunk := src.read(_DECRYPT_CHUNK_BYTES):
                dst.write(unpadder.update(decryptor.update(chunk)))
            dst.write(unpadder.update(decryptor.finalize()) + unpadder.finalize())
    except ValueError as error:
        raise WeixinMediaError(f"PKCS7 unpadding failed: {error}") from error
    return destination.stat().st_size


def format_size(num_bytes: int) -> str:
    """Render a byte count for user-facing notes, e.g. ``250.5 MB`` or ``2.1 GB``."""
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{round(num_bytes / (1024 * 1024 * 1024), 1):g} GB"
    return f"{round(num_bytes / (1024 * 1024), 1):g} MB"


def sniff_image_mime(data: bytes) -> str:
    """Detect an image media type from decrypted bytes; octet-stream when unknown."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format or "", "")
    except (UnidentifiedImageError, OSError):
        return "application/octet-stream"
    return mime or "application/octet-stream"


def guess_file_mime(filename: str) -> str:
    """Map a filename to a media type, mirroring the official extension table."""
    suffix = "." + filename.rsplit(".", maxsplit=1)[-1].lower() if "." in filename else ""
    mime = _EXTENSION_TO_MIME.get(suffix) or mimetypes.types_map.get(suffix)
    return mime or "application/octet-stream"


def extension_for_mime(mime: str) -> str:
    """Return a canonical extension for a sniffed media type."""
    return _MIME_TO_EXTENSION.get(mime) or mimetypes.guess_extension(mime) or ".bin"


def safe_filename(value: str) -> str:
    """Clamp a media filename to a filesystem-safe basename."""
    return safe_attachment_filename(value, fallback="weixin-attachment")

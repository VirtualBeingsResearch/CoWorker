from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from coworker.channels.weixin.media import (
    WeixinMediaError,
    aes_ecb_padded_size,
    decrypt_aes_ecb,
    decrypt_aes_ecb_file,
    encode_outbound_aes_key,
    encrypt_aes_ecb,
    encrypt_aes_ecb_file,
    extension_for_mime,
    format_size,
    guess_file_mime,
    resolve_media_key,
    safe_filename,
    sniff_image_mime,
)


def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    return encrypt_aes_ecb(plaintext, key)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (2, 2), color=(10, 20, 30)) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def test_resolve_media_key_accepts_official_key_encodings() -> None:
    raw_key = b"0123456789abcdef"
    hex_key = b"0123456789abcdef0987654321fedcba"

    assert resolve_media_key(None, base64.b64encode(raw_key).decode()) == raw_key
    assert (
        resolve_media_key(None, base64.b64encode(hex_key).decode("ascii")).hex()
        == hex_key.decode("ascii")
    )
    assert resolve_media_key(raw_key.hex(), None) == raw_key
    assert resolve_media_key(raw_key.hex(), "ignored") == raw_key
    assert resolve_media_key(None, None) is None


def test_resolve_media_key_rejects_malformed_keys() -> None:
    with pytest.raises(WeixinMediaError):
        resolve_media_key(None, "not-base64!!!")
    with pytest.raises(WeixinMediaError):
        resolve_media_key(None, base64.b64encode(b"short").decode())
    with pytest.raises(WeixinMediaError):
        resolve_media_key("zzzz", None)


def test_decrypt_aes_ecb_round_trip_and_padding_errors() -> None:
    key = b"0123456789abcdef"
    plaintext = b"payload" * 5

    assert decrypt_aes_ecb(_encrypt_aes_ecb(plaintext, key), key) == plaintext

    with pytest.raises(WeixinMediaError):
        decrypt_aes_ecb(b"short", key)
    with pytest.raises(WeixinMediaError):
        decrypt_aes_ecb(_encrypt_aes_ecb(plaintext, key), b"fedcba9876543210")
    with pytest.raises(WeixinMediaError):
        decrypt_aes_ecb(b"\x00" * 16, key)


def test_decrypt_aes_ecb_file_streams_large_payload(tmp_path: Path) -> None:
    key = b"0123456789abcdef"
    plaintext = (b"weixin-media-payload" * 60_000)[: 3 * 1024 * 1024 + 7]
    source = tmp_path / "payload.enc"
    source.write_bytes(_encrypt_aes_ecb(plaintext, key))
    destination = tmp_path / "payload.bin"

    size = decrypt_aes_ecb_file(source, destination, key)

    assert size == len(plaintext)
    assert destination.read_bytes() == plaintext
    bad_source = tmp_path / "bad.enc"
    bad_source.write_bytes(b"not block aligned")
    with pytest.raises(WeixinMediaError):
        decrypt_aes_ecb_file(bad_source, tmp_path / "bad.bin", key)


def test_format_size_uses_readable_units() -> None:
    assert format_size(200 * 1024 * 1024) == "200 MB"
    assert format_size(300 * 1024 * 1024 + 512 * 1024) == "300.5 MB"
    assert format_size(3 * 1024 * 1024 * 1024 + 200 * 1024 * 1024) == "3.2 GB"
    assert format_size(0) == "0 MB"


def test_sniff_image_mime_detects_png_and_falls_back() -> None:
    assert sniff_image_mime(_png_bytes()) == "image/png"
    assert sniff_image_mime(b"definitely not an image") == "application/octet-stream"


def test_guess_file_mime_and_extension_helpers() -> None:
    assert guess_file_mime("report.pdf") == "application/pdf"
    assert (
        guess_file_mime("book.docx")
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert guess_file_mime("unknown.zzz") == "application/octet-stream"
    assert extension_for_mime("image/jpeg") == ".jpg"
    assert extension_for_mime("application/x-unknown") in {".bin", ".unknown"}


@pytest.mark.parametrize(
    ("size", "padded"),
    [(0, 16), (15, 16), (16, 32), (17, 32)],
)
def test_aes_ecb_padded_size(size: int, padded: int) -> None:
    assert aes_ecb_padded_size(size) == padded


def test_encode_outbound_aes_key_matches_inbound_decoder() -> None:
    key = b"0123456789abcdef"

    encoded = encode_outbound_aes_key(key)

    assert base64.b64decode(encoded) == key.hex().encode("ascii")
    assert resolve_media_key(None, encoded) == key


def test_encrypt_aes_ecb_file_round_trip(tmp_path: Path) -> None:
    key = b"0123456789abcdef"
    plaintext = b"weixin-upload"
    source = tmp_path / "plain.bin"
    source.write_bytes(plaintext)
    ciphertext = tmp_path / "cipher.bin"
    restored = tmp_path / "restored.bin"

    assert encrypt_aes_ecb_file(source, ciphertext, key) == aes_ecb_padded_size(len(plaintext))
    assert decrypt_aes_ecb_file(ciphertext, restored, key) == len(plaintext)
    assert restored.read_bytes() == plaintext


def test_safe_filename_clamps_and_flattens() -> None:
    assert safe_filename(r"..\..\evil.png") == "evil.png"
    long_name = "x" * 300 + ".png"
    clamped = safe_filename(long_name)
    assert len(clamped) == 180
    assert clamped.endswith(".png")
    assert safe_filename("") == "weixin-attachment"

"""WeChat media — AES-128-ECB encryption + CDN upload/download.

Ported verbatim from ``wechat_channel/media.py``.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .ilink import ILinkClient, MSG_FILE, MSG_IMAGE, MSG_VIDEO

# Media type mapping for getuploadurl
UPLOAD_IMAGE = 1
UPLOAD_VIDEO = 2
UPLOAD_FILE = 3


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    padder = sym_padding.PKCS7(block_size * 8).padder()
    return padder.update(data) + padder.finalize()


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    unpadder = sym_padding.PKCS7(block_size * 8).unpadder()
    return unpadder.update(data) + unpadder.finalize()


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    padded = _pkcs7_pad(plaintext)
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt and remove PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return _pkcs7_unpad(padded)


def cipher_size(plaintext_size: int) -> int:
    """Ciphertext size after AES-ECB + PKCS7 padding."""
    return math.ceil((plaintext_size + 1) / 16) * 16


async def upload_media(
    client: ILinkClient,
    file_path: Path,
    media_type: int,
    to_user_id: str = "",
) -> dict:
    """Upload a file to WeChat CDN with AES encryption.

    Returns a CDN reference dict: ``{"encrypt_query_param", "aes_key", ...}``.
    """
    data = file_path.read_bytes()
    raw_size = len(data)
    raw_md5 = hashlib.md5(data).hexdigest()

    aes_key = os.urandom(16)
    aes_key_hex = aes_key.hex()

    encrypted = encrypt_aes_ecb(data, aes_key)
    enc_size = len(encrypted)

    filekey = os.urandom(16).hex()

    upload_info = await client.get_upload_url(
        filekey=filekey,
        media_type=media_type,
        raw_size=raw_size,
        raw_md5=raw_md5,
        cipher_size=enc_size,
        aes_key_hex=aes_key_hex,
        to_user_id=to_user_id,
        no_need_thumb=True,
    )

    upload_url = upload_info.get("upload_full_url", "")
    if not upload_url:
        raise ValueError(f"No upload URL in response: {upload_info}")

    # POST encrypted data to CDN (not PUT!)
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.post(
            upload_url, content=encrypted,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()

    encrypt_query_param = resp.headers.get("x-encrypted-param", "")

    return {
        "encrypt_query_param": encrypt_query_param,
        "aes_key": aes_key_hex,
        "filekey": filekey,
        "raw_size": raw_size,
        "cipher_size": enc_size,
    }


async def download_media(
    client: ILinkClient, media_info: dict, dest_dir: Path
) -> Path:
    """Download and decrypt a media file from WeChat CDN."""
    import base64 as _b64

    encrypt_query = media_info.get("encrypt_query_param", "")
    aes_key_b64 = media_info.get("aes_key", "")

    # Decode AES key — two encodings observed:
    # base64(16 raw bytes) for images
    # base64(hex string) for files/voice/video
    try:
        decoded = _b64.b64decode(aes_key_b64)
        if len(decoded) == 16:
            aes_key = decoded
        else:
            aes_key = bytes.fromhex(decoded.decode())
    except Exception:
        aes_key = bytes.fromhex(aes_key_b64)

    download_url = f"{client.cdn_url}/download?encrypted_query_param={quote(encrypt_query)}"

    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.get(download_url)
        resp.raise_for_status()
        encrypted = resp.content

    plaintext = decrypt_aes_ecb(encrypted, aes_key)

    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = _guess_extension(
        media_info.get("type", 0), media_info.get("filename", "")
    )
    dest = dest_dir / f"wechat-media-{int(time.time() * 1000)}{ext}"
    dest.write_bytes(plaintext)

    return dest


def _guess_extension(msg_type: int, filename: str) -> str:
    """Guess file extension from message type or filename."""
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return {
        MSG_IMAGE: ".jpg",
        3: ".silk",   # voice
        MSG_FILE: ".bin",
        MSG_VIDEO: ".mp4",
    }.get(msg_type, ".bin")

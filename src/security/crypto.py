"""AES-GCM encryption for sensitive images and biometric vectors at rest."""
import base64
import binascii
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MZ01"


def data_key_bytes(encoded: str) -> bytes:
    if not encoded:
        raise RuntimeError("MUZZLEID_DATA_KEY is not configured.")
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise RuntimeError("MUZZLEID_DATA_KEY is invalid.") from exc
    if len(key) != 32:
        raise RuntimeError("MUZZLEID_DATA_KEY must encode exactly 32 random bytes.")
    return key


def encrypt_bytes(plaintext: bytes, key: bytes, associated_data: str) -> bytes:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data.encode("utf-8"))
    return MAGIC + nonce + ciphertext


def decrypt_bytes(envelope: bytes, key: bytes, associated_data: str) -> bytes:
    if not envelope.startswith(MAGIC) or len(envelope) < len(MAGIC) + 12 + 16:
        raise ValueError("Encrypted record has an invalid format.")
    nonce = envelope[len(MAGIC):len(MAGIC) + 12]
    ciphertext = envelope[len(MAGIC) + 12:]
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data.encode("utf-8"))

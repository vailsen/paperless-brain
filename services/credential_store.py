# services/credential_store.py
"""Encrypted per-user credential storage.

Credentials are stored in {app_path}/data/credentials/{username}.enc.
Files are encrypted with AES-256-GCM under a key derived (HKDF-SHA256) from the
user's Paperless token. The token is the key material — if the Paperless token
is regenerated the file cannot be decrypted and settings must be re-entered.

Format (v2): b"PS2\\x00" + nonce (12) + AES-GCM ciphertext+tag.
Legacy files (SHAKE-256 keystream + HMAC-SHA-256, no magic prefix) are still
readable; the next save_credentials() rewrites them in the new format.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config.settings import settings

_CRED_DIR = Path(settings.app_path) / "data" / "credentials"
_CRED_DIR.mkdir(parents=True, exist_ok=True)

_MAGIC = b"PS2\x00"
_KEY_INFO = b"papersage:credstore:v2"


# ── Crypto (v2: AES-256-GCM) ──────────────────────────────────────────────────


def _derive_key(token: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO
    ).derive(token.encode())


def _encrypt(plaintext: bytes, token: str) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(_derive_key(token)).encrypt(nonce, plaintext, _MAGIC)
    return _MAGIC + nonce + ct


def _decrypt(data: bytes, token: str) -> bytes:
    if data.startswith(_MAGIC):
        body = data[len(_MAGIC):]
        nonce, ct = body[:12], body[12:]
        try:
            return AESGCM(_derive_key(token)).decrypt(nonce, ct, _MAGIC)
        except InvalidTag:
            raise ValueError("Credential file corrupted or token mismatch")
    return _decrypt_legacy(data, token)


# ── Legacy format (pre-v2), read-only ─────────────────────────────────────────


def _derive_keys_legacy(token: str) -> tuple[bytes, bytes]:
    master = hashlib.sha256(token.encode()).digest()
    enc_key = hashlib.sha256(b"papersage:enc:" + master).digest()
    mac_key = hashlib.sha256(b"papersage:mac:" + master).digest()
    return enc_key, mac_key


def _decrypt_legacy(data: bytes, token: str) -> bytes:
    enc_key, mac_key = _derive_keys_legacy(token)
    mac, ct = data[:32], data[32:]
    expected = hmac.new(mac_key, ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("Credential file corrupted or token mismatch")
    nonce, ciphertext = ct[:16], ct[16:]
    ks = hashlib.shake_256(enc_key + nonce).digest(len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, ks))


# ── Public API ────────────────────────────────────────────────────────────────


def save_credentials(username: str, token: str, creds: dict) -> None:
    plaintext = json.dumps(creds, ensure_ascii=False).encode("utf-8")
    encrypted = _encrypt(plaintext, token)
    path = _CRED_DIR / f"{username}.enc"
    path.write_bytes(encrypted)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_credentials(username: str, token: str) -> dict:
    path = _CRED_DIR / f"{username}.enc"
    if not path.exists():
        return {}
    try:
        return json.loads(_decrypt(path.read_bytes(), token).decode("utf-8"))
    except Exception:
        return {}


def delete_credentials(username: str) -> None:
    path = _CRED_DIR / f"{username}.enc"
    if path.exists():
        path.unlink()

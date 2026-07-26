"""Credential store — AES-GCM roundtrip and failure modes.

IMAP/CalDAV passwords and API keys are encrypted with a key derived from the
user's session token. The properties that matter: a wrong token must never
decrypt, tampering must be detected (GCM auth tag), and every failure path must
return {} rather than raise — the settings page calls load_credentials() during
render, so an exception there would blank the whole page.
"""

import json

import pytest

from services import credential_store as cs

TOKEN = "session-token-aaaa"
OTHER_TOKEN = "session-token-bbbb"
CREDS = {
    "imap": {"host": "imap.example.com", "user": "alice", "password": "hunter2"},
    "llm": {"api_key": "sk-test-123"},
}


@pytest.fixture(autouse=True)
def _clean_user():
    """Each test starts and ends with no stored credentials."""
    cs.delete_credentials("testuser")
    yield
    cs.delete_credentials("testuser")


def test_roundtrip_preserves_the_payload():
    cs.save_credentials("testuser", TOKEN, CREDS)
    assert cs.load_credentials("testuser", TOKEN) == CREDS


def test_ciphertext_does_not_contain_the_plaintext():
    """The whole point: a stolen file must not leak the password."""
    cs.save_credentials("testuser", TOKEN, CREDS)
    blob = (cs._CRED_DIR / "testuser.enc").read_bytes()
    assert b"hunter2" not in blob
    assert b"sk-test-123" not in blob
    assert b"imap.example.com" not in blob


def test_wrong_token_does_not_decrypt():
    cs.save_credentials("testuser", TOKEN, CREDS)
    assert cs.load_credentials("testuser", OTHER_TOKEN) == {}


def test_tampered_ciphertext_is_rejected():
    """GCM auth tag must catch modification rather than yield garbage."""
    cs.save_credentials("testuser", TOKEN, CREDS)
    path = cs._CRED_DIR / "testuser.enc"
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0xFF  # flip a bit in the auth tag
    path.write_bytes(bytes(blob))
    assert cs.load_credentials("testuser", TOKEN) == {}


def test_missing_file_returns_empty_dict():
    assert cs.load_credentials("nobody-at-all", TOKEN) == {}


def test_garbage_file_returns_empty_dict():
    """A truncated or foreign file must not crash the settings page."""
    (cs._CRED_DIR / "testuser.enc").write_bytes(b"not an encrypted blob")
    assert cs.load_credentials("testuser", TOKEN) == {}


def test_save_overwrites_previous_credentials():
    cs.save_credentials("testuser", TOKEN, {"imap": {"user": "old"}})
    cs.save_credentials("testuser", TOKEN, {"imap": {"user": "new"}})
    assert cs.load_credentials("testuser", TOKEN)["imap"]["user"] == "new"


def test_delete_removes_the_file():
    cs.save_credentials("testuser", TOKEN, CREDS)
    assert (cs._CRED_DIR / "testuser.enc").exists()
    cs.delete_credentials("testuser")
    assert not (cs._CRED_DIR / "testuser.enc").exists()
    assert cs.load_credentials("testuser", TOKEN) == {}


def test_delete_is_idempotent():
    cs.delete_credentials("never-existed")  # must not raise


def test_two_saves_produce_different_ciphertext():
    """A fresh nonce per save — identical output would leak equality of secrets."""
    cs.save_credentials("testuser", TOKEN, CREDS)
    first = (cs._CRED_DIR / "testuser.enc").read_bytes()
    cs.save_credentials("testuser", TOKEN, CREDS)
    second = (cs._CRED_DIR / "testuser.enc").read_bytes()
    assert first != second


def test_unicode_survives_the_roundtrip():
    creds = {"imap": {"user": "müller", "password": "paßwörtchen–✓"}}
    cs.save_credentials("testuser", TOKEN, creds)
    assert cs.load_credentials("testuser", TOKEN) == creds


def test_stored_file_is_not_world_readable():
    """Credentials at rest must not be readable by other users on the host."""
    cs.save_credentials("testuser", TOKEN, CREDS)
    mode = (cs._CRED_DIR / "testuser.enc").stat().st_mode & 0o077
    assert mode == 0, f"file is group/other accessible: {oct(mode)}"


def test_empty_credentials_roundtrip():
    cs.save_credentials("testuser", TOKEN, {})
    assert cs.load_credentials("testuser", TOKEN) == {}

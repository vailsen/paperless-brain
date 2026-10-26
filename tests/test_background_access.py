"""Background access: a user's token, kept only on their explicit grant."""

import pytest

from services import background_access as ba


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "_dir", lambda: tmp_path / "background_access")


def test_nothing_is_stored_without_a_grant():
    assert ba.token_for("alice") is None
    assert ba.status("alice") == "none"
    ba.refresh_on_login("alice", "tok-1")          # login alone never grants
    assert ba.status("alice") == "none"


def test_grant_and_revoke():
    ba.grant("alice", "tok-1")
    assert ba.token_for("alice") == "tok-1" and ba.status("alice") == "active"
    ba.revoke("alice")
    assert ba.token_for("alice") is None and ba.status("alice") == "none"
    ba.revoke("alice")                              # idempotent


def test_the_token_is_not_stored_in_clear(tmp_path):
    ba.grant("alice", "very-secret-token-123")
    raw = ba._path("alice").read_bytes()
    assert b"very-secret-token-123" not in raw
    assert oct(ba._path("alice").stat().st_mode & 0o777) == "0o600"


def test_a_grant_copied_to_another_name_does_not_open():
    ba.grant("alice", "tok-1")
    ba._path("mallory").write_bytes(ba._path("alice").read_bytes())
    assert ba.token_for("mallory") is None


def test_a_rejected_token_waits_for_the_next_login():
    ba.grant("alice", "tok-old")
    ba.mark_needs_reauth("alice")
    assert ba.status("alice") == "needs_reauth" and ba.token_for("alice") is None
    ba.refresh_on_login("alice", "tok-new")         # regenerated in Paperless
    assert ba.status("alice") == "active" and ba.token_for("alice") == "tok-new"


def test_a_rotated_storage_secret_reads_as_no_grant(monkeypatch):
    ba.grant("alice", "tok-1")
    monkeypatch.setattr(ba, "_key", lambda: b"\x00" * 32)
    assert ba.token_for("alice") is None and ba.status("alice") == "none"


def test_grant_needs_a_token():
    with pytest.raises(ValueError):
        ba.grant("alice", "")

"""The reindex trigger: schema version recorded per user vs. the code's.

An index built with an older embedding format is not comparable to a new one,
so the mismatch has to force a full rebuild — exactly once, not on every sync.
"""

import pytest

from vault import sync as vault_sync
from vault.context import EMBEDDING_SCHEMA_VERSION


@pytest.fixture
def git_dir(tmp_path, monkeypatch):
    d = tmp_path / "vault_git" / "alice"
    d.mkdir(parents=True)
    monkeypatch.setattr(vault_sync, "git_dir_path", lambda _u: d)
    return d


def test_unknown_user_needs_a_reindex(git_dir):
    assert vault_sync._recorded_schema_version("alice") == 0
    assert vault_sync._needs_reindex("alice")


def test_legacy_migration_marker_counts_as_version_1(git_dir):
    """An install that already ran the psage_id migration is at v1, not v0."""
    (git_dir / vault_sync._PBRAIN_MIGRATION_MARKER).write_text("done")
    assert vault_sync._recorded_schema_version("alice") == 1


def test_marking_current_stops_the_reindex(git_dir):
    vault_sync._mark_schema_current("alice")
    assert vault_sync._recorded_schema_version("alice") == EMBEDDING_SCHEMA_VERSION
    assert not vault_sync._needs_reindex("alice")


def test_marking_current_also_writes_the_legacy_marker(git_dir):
    """A downgrade must not re-run the old id migration on a migrated index."""
    vault_sync._mark_schema_current("alice")
    assert (git_dir / vault_sync._PBRAIN_MIGRATION_MARKER).exists()


def test_older_recorded_version_triggers_a_reindex(git_dir):
    (git_dir / vault_sync._SCHEMA_MARKER).write_text(str(EMBEDDING_SCHEMA_VERSION - 1))
    assert vault_sync._needs_reindex("alice")


def test_newer_recorded_version_does_not(git_dir):
    """Rolling back the app must not wipe an index built by a newer build."""
    (git_dir / vault_sync._SCHEMA_MARKER).write_text(str(EMBEDDING_SCHEMA_VERSION + 1))
    assert not vault_sync._needs_reindex("alice")


def test_corrupt_marker_is_treated_as_never_indexed(git_dir):
    (git_dir / vault_sync._SCHEMA_MARKER).write_text("garbage")
    assert vault_sync._recorded_schema_version("alice") == 0

"""The chat drawer's grouping and search (`app_ui/conv_groups.py`,
`chat_history_service.search_conversations`)."""

from datetime import datetime, timedelta, timezone

from app_ui import conv_groups as cg

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)


def _c(i, days, expert=None, title="t", context=None):
    return {"id": str(i), "title": title, "expert_id": expert, "context": context,
            "updated_at": (NOW - timedelta(days=days)).isoformat()}


def test_by_date_in_order_and_without_empty_sections():
    convs = [_c(1, 0), _c(2, 1), _c(3, 3), _c(4, 40)]
    groups = cg.group(convs, "date", NOW)
    assert [(g.key, [c["id"] for c in g.convs]) for g in groups] == [
        ("today", ["1"]), ("yesterday", ["2"]), ("week", ["3"]), ("older", ["4"])]


def test_by_expert_keeps_the_newest_section_first_and_universal_for_none():
    convs = [_c(1, 0, "tax"), _c(2, 1, None), _c(3, 2, "tax"), _c(4, 3, "universal")]
    groups = cg.group(convs, "expert", NOW)
    assert [(g.key, [c["id"] for c in g.convs]) for g in groups] == [
        ("tax", ["1", "3"]), ("universal", ["2", "4"])]


def test_search_matches_title_or_content_hits():
    a, b = _c(1, 0, title="Kfz-Versicherung"), _c(2, 0, title="Steuer")
    assert cg.matches(a, "versich", set())
    assert not cg.matches(b, "versich", set())
    assert cg.matches(b, "versich", {"2"})
    assert cg.matches(b, "  ", set())


def test_context_kind():
    assert cg.context_kind(_c(1, 0, context="finding:abc")) == "finding"
    assert cg.context_kind(_c(1, 0, context="dossier:x")) == "dossier"
    assert cg.context_kind(_c(1, 0, context="findings:overview")) == "overview"
    assert cg.context_kind(_c(1, 0)) == ""


def test_content_search_is_per_user_and_folds_umlauts(tmp_path, monkeypatch):
    import sqlite3

    import services.chat_history_service as hist

    db = tmp_path / "w.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE chat_conversations (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at TEXT,"
        " updated_at TEXT);"
        "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT"
        " REFERENCES chat_conversations(id) ON DELETE CASCADE, user_id TEXT, role TEXT, content TEXT,"
        " created_at TEXT);")
    conn.close()
    monkeypatch.setattr(hist, "_DB_PATH", str(db))
    monkeypatch.setattr(hist, "_migrated", False)
    a = hist.create_conversation("alice", "x")
    b = hist.create_conversation("bob", "y")
    hist.append_messages(a, "alice", [{"role": "user", "content": "Termin bei der Ärztin"}])
    hist.append_messages(b, "bob", [{"role": "user", "content": "Ärztin auch hier"}])
    assert hist.search_conversations("alice", "ärztin") == {a}
    assert hist.search_conversations("alice", "100%") == set()
    assert hist.search_conversations("alice", "") == set()

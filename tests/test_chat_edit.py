"""Editing a stored conversation (`services/chat_edit.py`)."""

from services import chat_edit as ce


def _m(role, text):
    return {"role": role, "content": text}


def test_a_note_is_the_users_words_framed_as_a_note():
    note = ce.make_note("  Ignore the 2023 contract.  ")
    assert note["role"] == "user" and ce.is_note(note)
    assert ce.note_text(note["content"]) == "Ignore the 2023 contract."
    sent = ce.for_model([_m("user", "q"), _m("assistant", "a"), note, _m("user", "next?")])
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"].startswith(ce.NOTE_PREFIX)
    assert "Ignore the 2023 contract." in sent[-1]["content"]
    assert sent[-1]["content"].endswith("next?")  # joined with the question after it


def test_plain_messages_are_not_notes():
    assert ce.note_text("Hello <pb-note>x</pb-note> there") is None
    assert not ce.is_note(_m("assistant", "<pb-note>x</pb-note>"))


def test_for_model_joins_neighbours_and_opens_with_the_user():
    sent = ce.for_model([_m("assistant", "summary"), _m("assistant", "more"), _m("user", "q1"), _m("user", "q2")])
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == ce.OPENING
    assert sent[1]["content"] == "summary\n\nmore"
    assert sent[2]["content"] == "q1\n\nq2"
    assert ce.for_model([]) == []


def test_remove_rekeys_provenance():
    msgs = [_m("user", "a"), _m("assistant", "A"), _m("user", "b"), _m("assistant", "B")]
    meta = {1: {"docs": [1]}, 3: {"docs": [3]}}
    new, new_meta, index_map = ce.remove(msgs, meta, {1, 2})
    assert [m["content"] for m in new] == ["a", "B"]
    assert new_meta == {1: {"docs": [3]}}  # B's provenance moved with it
    assert index_map == {0: 0, 3: 1}


def test_insert_shifts_everything_after():
    msgs = [_m("user", "a"), _m("assistant", "A")]
    new, new_meta, index_map = ce.insert(msgs, {1: {"x": 1}}, 1, ce.make_note("n"))
    assert [ce.is_note(m) for m in new] == [False, True, False]
    assert new_meta == {2: {"x": 1}}
    assert index_map == {0: 0, 1: 2}
    new, _meta, _map = ce.insert(msgs, {}, 99, ce.make_note("end"))
    assert ce.is_note(new[-1])


def test_compact_prompt_is_english_and_names_the_language():
    system, user = ce.compact_prompt([_m("user", "Wann endet der Vertrag?"), ce.make_note("Nur 2024")], "German")
    assert "Write it in German." in system
    assert "Write the summary in German." in user
    assert "User: Wann endet der Vertrag?" in user
    assert "Note by the user: Nur 2024" in user


def test_kept_histories_follow_their_answers():
    from app_ui import tool_trace as tt

    tt.keep(("u", "c", 1), {"a": 1})
    tt.keep(("u", "c", 3), {"b": 3})
    tt.keep(("u", "other", 1), {"x": 1})
    tt.rekey("u", "c", {0: 0, 3: 1})
    assert tt.kept(("u", "c", 1)) == {"b": 3}
    assert tt.kept(("u", "c", 3)) is None
    assert tt.kept(("u", "other", 1)) == {"x": 1}


def test_rewrite_keeps_provenance_and_pinning(tmp_path, monkeypatch):
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
    c = hist.create_conversation("alice", "x")
    msgs = [_m("user", "a"), _m("assistant", "A"), _m("user", "b"), _m("assistant", "B")]
    hist.append_messages(c, "alice", msgs)
    hist.set_turn_meta(c, "alice", 3, {"docs": [7]})
    new, meta, _map = ce.remove(msgs, hist.load_turn_meta(c, "alice"), {0, 1})
    hist.rewrite_conversation(c, "alice", new, meta)
    assert hist.load_messages(c, "alice") == new
    assert hist.load_turn_meta(c, "alice") == {1: {"docs": [7]}}
    assert not hist.get_conversation(c, "alice")["pinned"]
    hist.set_pinned(c, "alice", True)
    hist.set_expert(c, "alice", "tax")
    row = hist.get_conversation(c, "alice")
    assert row["pinned"] == 1 and row["expert_id"] == "tax"
    hist.set_pinned(c, "bob", False)  # someone else's id does nothing
    assert hist.get_conversation(c, "alice")["pinned"] == 1


def test_pin_folders(tmp_path, monkeypatch):
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
    c = hist.create_conversation("alice", "x")
    f = hist.create_pin_folder("alice", "  Taxes ")
    hist.create_pin_folder("alice", "car")
    assert [r["name"] for r in hist.list_pin_folders("alice")] == ["car", "Taxes"]
    assert hist.list_pin_folders("bob") == []

    hist.set_pin_folder(c, "alice", f)  # filing pins it
    row = hist.get_conversation(c, "alice")
    assert row["pinned"] == 1 and row["pin_folder"] == f
    bobs = hist.create_pin_folder("bob", "mine")
    hist.set_pin_folder(c, "alice", bobs)  # someone else's folder files nothing
    assert hist.get_conversation(c, "alice")["pin_folder"] == f

    hist.rename_pin_folder(f, "bob", "hijack")  # not bob's
    hist.rename_pin_folder(f, "alice", "Tax 2026")
    assert "Tax 2026" in [r["name"] for r in hist.list_pin_folders("alice")]

    hist.delete_pin_folder(f, "alice")  # its conversation stays pinned, loose
    row = hist.get_conversation(c, "alice")
    assert row["pinned"] == 1 and row["pin_folder"] is None

    hist.set_pin_folder(c, "alice", hist.list_pin_folders("alice")[0]["id"])
    hist.set_pinned(c, "alice", False)  # unpinning leaves the folder
    row = hist.get_conversation(c, "alice")
    assert row["pinned"] == 0 and row["pin_folder"] is None


def test_auto_title_never_replaces_the_users_own(tmp_path, monkeypatch):
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
    c = hist.create_conversation("alice", "and the other one?")
    before = hist.get_conversation(c, "alice")["updated_at"]
    assert hist.set_auto_title(c, "alice", "Car insurance renewal")
    row = hist.get_conversation(c, "alice")
    assert row["title"] == "Car insurance renewal" and row["updated_at"] == before
    assert not hist.set_auto_title(c, "bob", "hijack")
    hist.rename_conversation(c, "alice", "My car")
    assert not hist.set_auto_title(c, "alice", "Something else")
    assert hist.get_conversation(c, "alice")["title"] == "My car"

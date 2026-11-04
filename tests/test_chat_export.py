from datetime import datetime

from app_ui.chat_export import conversation_markdown


def test_conversation_export_has_every_turn_and_readable_refs():
    msgs = [
        {"role": "user", "content": "Was zahle ich in die Riester-Rente?"},
        {"role": "assistant", "content": "10.847,52 € [^32-p8]. Laut [^Cleveland Clinic] egal."},
        {"role": "system", "content": "hidden"},
        {"role": "assistant", "content": "  "},
    ]
    md = conversation_markdown(msgs, you="Du", assistant="Ärztin", now=datetime(2026, 11, 4, 9, 30))
    assert md.startswith("# Was zahle ich in die Riester-Rente?\n\n*2026-11-04 09:30*")
    assert "## Du\n\nWas zahle" in md and "## Ärztin\n\n10.847,52 € [#32 p8]. Laut (Cleveland Clinic) egal." in md
    assert "hidden" not in md and md.count("## ") == 2


def test_nothing_to_export():
    assert conversation_markdown([], you="You", assistant="A") == ""

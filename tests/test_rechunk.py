"""Chunk header v2, token-fitted chunks, and rebuilding chunks from sidecars."""

import asyncio
from datetime import datetime

from pipelines import rechunk
from services.chunking import (
    HEADER_VERSION,
    build_context_prefix,
    chunk_document,
    context_from_v1,
    first_sentence,
    strip_context_prefix,
)


def _tokens(text: str) -> int:
    """A stand-in tokenizer: two tokens a word, plus two special tokens."""
    return 2 * len(text.split()) + 2


# ── header ───────────────────────────────────────────────────────────────────


def test_header_has_title_context_and_first_summary_sentence():
    h = build_context_prefix("Rechnung", "Acme", datetime(2026, 7, 20), title="Laptop_Rechnung",
                             summary="Rechnung über einen Laptop. Zahlbar in 14 Tagen.")
    assert h == ("Dokument: Laptop Rechnung | Rechnung, Acme, 20.07.2026 — "
                 "Inhalt: Rechnung über einen Laptop. — ")


def test_header_without_title_is_the_old_prefix():
    assert build_context_prefix("Invoice", "Acme Ltd", datetime(2026, 7, 20)) == \
        "Dokument: Invoice, Acme Ltd, 20.07.2026 — "


def test_separator_inside_a_field_cannot_break_stripping():
    h = build_context_prefix("Brief", None, None, title="A — B", summary="Ein Brief — mit Strich, lang genug.")
    assert strip_context_prefix(h + "Der Text.") == "Der Text."


def test_strip_handles_both_versions_and_plain_text():
    assert strip_context_prefix("Dokument: Rechnung — Laptop 1299 €") == "Laptop 1299 €"
    assert strip_context_prefix("Dokument: T | R — Inhalt: S. — Body — mit Strich") == "Body — mit Strich"
    assert strip_context_prefix("Kein Header") == "Kein Header"
    assert strip_context_prefix(None) == ""


def test_context_of_a_version_1_chunk():
    assert context_from_v1("Dokument: Rechnung, Acme, 01.02.2025 — Text") == "Rechnung, Acme, 01.02.2025"
    assert context_from_v1("Text") == ""


def test_first_sentence_is_capped():
    assert first_sentence("Kurz. " + "x" * 300).startswith("x") is False
    assert len(first_sentence("a " * 300)) <= 202


# ── token budget ─────────────────────────────────────────────────────────────


def test_no_chunk_exceeds_the_window_header_included():
    header = "Dokument: " + "Titel " * 20 + "— "
    pages = ["Satz mit Worten. " * 400, "Zahl 12345 " * 300]
    chunks = chunk_document(pages, context_prefix=header, token_len=_tokens, max_tokens=512)
    assert len(chunks) > 5
    assert all(_tokens(c.text) <= 512 for c in chunks), [_tokens(c.text) for c in chunks]
    assert all(c.text.startswith(header) for c in chunks)


def test_token_budget_keeps_all_words():
    words = [f"w{i}" for i in range(3000)]
    chunks = chunk_document([" ".join(words)], context_prefix="", token_len=_tokens, max_tokens=300)
    seen = set()
    for c in chunks:
        seen.update(c.text.split())
    assert seen == set(words)


def test_without_token_len_the_word_budget_is_unchanged():
    pages = ["word " * 700]
    assert [c.text for c in chunk_document(pages)] == \
        [c.text for c in chunk_document(pages, token_len=None)]


# ── rebuild from the sidecar ─────────────────────────────────────────────────


class _Chroma:
    def __init__(self, items):
        self.items = {x["id"]: x for x in items}

    async def get(self, where=None, include=None):
        pid = where["paperless_id"]["$eq"]
        return [x for x in self.items.values() if x["metadata"]["paperless_id"] == pid]

    async def upsert(self, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.items[i] = {"id": i, "document": d, "metadata": m}

    async def delete(self, ids=None, where=None):
        for i in ids or []:
            self.items.pop(i, None)


class _Sidecars:
    def __init__(self, sidecar):
        self.sidecar = sidecar
        self.saved = None

    def load_sidecar(self, doc_id):
        return dict(self.sidecar)

    def save_sidecar(self, sidecar):
        self.saved = sidecar


class _Paperless:
    async def get_document(self, doc_id):
        class D:
            title = "Mietvertrag Wohnung"
        return D()


def _v1_chunks(n):
    return [{"id": f"paperless_7_chunk_{i}", "document": f"Dokument: Mietvertrag, Hausverwaltung, 01.02.2020 — alt {i}",
             "metadata": {"paperless_id": 7, "chunk_index": i, "page_number": 1}} for i in range(n)]


def test_rechunk_rewrites_header_and_drops_extra_chunks(monkeypatch):
    monkeypatch.setattr(rechunk, "_budget", lambda: (_tokens, 512))
    chroma = _Chroma(_v1_chunks(5) + [{"id": "paperless_8_chunk_0", "document": "x",
                                       "metadata": {"paperless_id": 8, "chunk_index": 0}}])
    side = _Sidecars({
        "paperless_id": 7, "full_text": "", "full_summary": "", "chunks": 5, "prompt_version": "0.6",
        "full_summary_summarized": "Mietvertrag für eine Wohnung in Karlsruhe. Miete 900 €.",
        "pages": [{"page_number": 1, "page_text": "Die Miete beträgt 900 € im Monat.", "page_summary": "",
                   "images": [{"description": "Unterschrift", "kind": "signature"}]}],
        "tables": [], "actions": [], "cross_refs": [],
    })
    n = asyncio.run(rechunk.rechunk_document(7, _Paperless(), chroma, side))
    assert n == 1
    doc7 = [x for x in chroma.items.values() if x["metadata"]["paperless_id"] == 7]
    assert [x["id"] for x in doc7] == ["paperless_7_chunk_0"]
    text, meta = doc7[0]["document"], doc7[0]["metadata"]
    assert text.startswith("Dokument: Mietvertrag Wohnung | Mietvertrag, Hausverwaltung, 01.02.2020 — "
                           "Inhalt: Mietvertrag für eine Wohnung in Karlsruhe. — ")
    assert "Die Miete beträgt 900 €" in text
    assert meta["header"] == HEADER_VERSION and meta["context"] == "Mietvertrag, Hausverwaltung, 01.02.2020"
    assert "paperless_8_chunk_0" in chroma.items  # another document untouched
    assert side.saved is not None and side.saved.chunks == 1


def test_outdated_ids_are_the_documents_below_the_header_version():
    class C:
        async def get(self, include=None):
            return [{"metadata": {"paperless_id": 1}}, {"metadata": {"paperless_id": 2, "header": HEADER_VERSION}},
                    {"metadata": {"paperless_id": 3, "header": HEADER_VERSION}}, {"metadata": {"paperless_id": 3}}]

    assert asyncio.run(rechunk.outdated_ids(C())) == [3, 1]


def test_nothing_to_rebuild_without_sidecar():
    side = _Sidecars(None)
    side.load_sidecar = lambda doc_id: None
    assert asyncio.run(rechunk.rechunk_document(7, _Paperless(), _Chroma(_v1_chunks(1)), side)) is None

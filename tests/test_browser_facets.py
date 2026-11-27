"""The browser's in-memory facets (`app_ui/doc_facets.py`) and filter-first
semantic search (`pipelines/search.search(candidates=…)`)."""

import asyncio
from datetime import date, datetime

from starlette.datastructures import QueryParams

from app_ui import doc_facets as fx
from models.paperless_document import PaperlessDocument

TODAY = date(2026, 11, 27)


def _doc(i, *, tags=(), corr=None, typ=None, created="2026-01-15", added="2026-01-20", title=""):
    return PaperlessDocument.model_construct(
        id=i, title=title or f"Doc {i}", tags=list(tags), correspondent=corr, document_type=typ, owner_name="alice",
        created=datetime.fromisoformat(created), added=datetime.fromisoformat(added))


DOCS = [
    _doc(1, tags=["Steuer", "Bank"], corr="Finanzamt", typ="Letter", created="2025-03-02"),
    _doc(2, tags=["Steuer"], corr="Finanzamt", typ="Invoice", created="2026-03-10"),
    _doc(3, tags=["Bank"], corr="Sparkasse", typ="Statement", created="2026-04-01", added="2026-11-25"),
    _doc(4, tags=[], corr=None, typ="Invoice", created="2026-04-02", title="Zahnarzt Rechnung"),
]


def _ids(docs):
    return [d.id for d in docs]


def test_values_or_within_a_facet_and_across_facets():
    f = fx.Filters()
    f.cycle("tags", "Steuer")
    f.cycle("tags", "Bank")
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [1, 2, 3]
    f.cycle("correspondent", "Finanzamt")
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [1, 2]


def test_tags_all_and_exclusion():
    f = fx.Filters(tags_all=True)
    f.cycle("tags", "Steuer")
    f.cycle("tags", "Bank")
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [1]
    g = fx.Filters()
    g.cycle("tags", "Bank")
    g.cycle("tags", "Bank")  # second tap: exclude
    assert g.state("tags", "Bank") == -1
    assert _ids(fx.apply(DOCS, g, today=TODAY)) == [2, 4]
    g.cycle("tags", "Bank")  # third tap: cleared
    assert g.sel == {}


def test_no_correspondent_is_a_value():
    f = fx.Filters()
    f.cycle("correspondent", fx.NONE)
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [4]


def test_created_years_and_months():
    f = fx.Filters()
    f.cycle("created", "2026")
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [2, 3, 4]
    f.cycle("created", "2026-04")  # a month narrows its year: the year itself goes
    assert f.sel["created"] == {"2026-04": 1}
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [3, 4]


def test_added_preset_and_text():
    f = fx.Filters(added="7d")
    assert _ids(fx.apply(DOCS, f, today=TODAY)) == [3]
    g = fx.Filters(text="zahnarzt")
    assert _ids(fx.apply(DOCS, g, today=TODAY)) == [4]
    assert _ids(fx.apply(DOCS, fx.Filters(text="#2"), today=TODAY)) == [2]


def test_counts_are_disjunctive():
    f = fx.Filters()
    f.cycle("tags", "Steuer")
    c = fx.counts(DOCS, f, today=TODAY)
    # Tags ignore the tag choice: Bank still shows what it would add.
    assert c["tags"]["Bank"] == 2 and c["tags"]["Steuer"] == 2
    # Other facets count inside it.
    assert c["correspondent"]["Finanzamt"] == 2 and c["correspondent"]["Sparkasse"] == 0


def test_flags_are_their_own_facet():
    f = fx.Filters()
    f.cycle(fx.FLAG_FACET, "actions")
    assert _ids(fx.apply(DOCS, f, flags={3: {"actions"}}, today=TODAY)) == [3]


def test_query_round_trip():
    f = fx.Filters(tags_all=True, added="30d", text="abc")
    f.cycle("tags", "Steuer")
    f.cycle("tags", "Bank")
    f.cycle("tags", "Bank")
    f.cycle("correspondent", fx.NONE)
    q = fx.to_query(f, mode="title", q="abc", view="list", sort="title")
    g, extra = fx.from_query(QueryParams(q))
    assert g.sel == f.sel and g.tags_all and g.added == "30d" and g.text == "abc"
    assert extra == {"m": "title", "q": "abc", "v": "list", "s": "title"}


# ── Filter first, then rank ───────────────────────────────────────────────────

class _Chroma:
    where = None

    async def query(self, query_texts, n_results, where=None, where_document=None, where_pending=None):
        self.where = where
        ids = where["paperless_id"]["$in"]
        return [[{"metadata": {"paperless_id": i, "page_number": 1}, "distance": 0.1 * n, "document": "x"}
                 for n, i in enumerate(ids)]]


class _Pl:
    async def get_tag_map(self):
        return {7: "Rechnung"}

    async def list_documents(self, tag_ids_any=None, **kw):
        # Every document tagged "Rechnung" — also outside the caller's filter.
        return [_doc(i, tags=["Rechnung"]) for i in (2, 9)]

    async def visible_ids(self):
        return {1, 2, 3, 4, 9}


def test_semantic_search_ranks_only_the_candidates(monkeypatch):
    import pipelines.search as ps

    fake = _Chroma()
    monkeypatch.setattr(ps, "chroma", fake)
    monkeypatch.setattr(ps.sidecar_service, "has_actions", lambda _i: False)
    cands = [DOCS[0], DOCS[1]]
    res = asyncio.run(ps.search({}, semantic_query="rechnung", paperless_client=_Pl(), candidates=cands))
    assert fake.where == {"paperless_id": {"$in": [1, 2]}}
    # Document 9 carries the tag "Rechnung" but is outside the filter: it must
    # not ride in on the tag-name match.
    assert sorted(r.document.id for r in res) == [1, 2]


def test_filtered_tag_hits_stay_inside_the_filter(monkeypatch):
    """The chat's path (filters, no candidates) has the same rule."""
    import pipelines.search as ps

    fake = _Chroma()
    monkeypatch.setattr(ps, "chroma", fake)
    monkeypatch.setattr(ps.sidecar_service, "has_actions", lambda _i: False)

    class _PlF(_Pl):
        async def list_documents(self, tag_ids_any=None, correspondent=None, **kw):
            if tag_ids_any:
                return [_doc(i, tags=["Rechnung"]) for i in (2, 9)]
            return [DOCS[0], DOCS[1]]  # what the correspondent filter returns

    res = asyncio.run(ps.search({"correspondent": [1]}, semantic_query="rechnung", paperless_client=_PlF()))
    assert 9 not in {r.document.id for r in res}

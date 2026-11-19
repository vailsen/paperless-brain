"""search_exact's literal match: an identifier is found however it is spelled.

The patterns go to Chroma's `$regex` (Rust regex); the subset used here — `(?i)`,
character classes, escapes — means the same in Python's `re`, so the tests
check the generated pattern against text directly.
"""

import asyncio
import re

import pytest

from services.chat_service import _build_where_document, _code_runs, literal_variants


def _patterns(query: str) -> list[str]:
    wd = _build_where_document(query)
    assert wd is not None
    return [c["$regex"] for c in wd["$or"]] if "$or" in wd else [wd["$regex"]]


def _matches(query: str, text: str) -> bool:
    return any(re.search(p, text) for p in _patterns(query))


@pytest.mark.parametrize(
    "query,text",
    [
        # IBAN with and without the 4-blocks, either way round
        ("DE89 3704 0044 0532 0130 00", "IBAN: DE89370400440532013000 BIC"),
        ("DE89370400440532013000", "IBAN DE89 3704 0044 0532 0130 00"),
        ("de89 3704 0044 0532 0130 00", "IBAN: DE89370400440532013000"),
        # letter prefix + long number (was a word OR: 'VR' matched everything)
        ("VR 1000631423", "Rechnungsnummer VR 1000631423 vom"),
        ("VR 1000631423", "Rechnungsnummer VR1000631423"),
        ("vr1000631423", "Rechnungsnummer VR 1000631423"),
        # a noun before the number is not part of what must match
        ("Rechnung Nr. 00206538856/25/03", "Rechnungsnummer: 00206538856/25/03"),
        ("Kundennummer 6965898", "Kd.-Nr. 6965898 VKM"),
        # licence plate, every separator
        ("KA-ZY 244", "Kennzeichen KA ZY244"),
        ("ka zy 244", "Kennzeichen KA-ZY-244"),
        # tax number and file references with slashes and spaces
        ("65822/61741", "Steuernummer 65822 / 61741"),
        ("MAU044 GRG 915 / 2021", "Unser Zeichen MAU044 GRG 915 / 2021 Bretten"),
        ("924045335", "Kfz-Versicherung Nr. 924 045 335"),
        ("102017109924.7", "Aktenzeichen 10 2017 109 924.7"),
    ],
)
def test_identifier_found_in_any_spelling(query, text):
    assert _matches(query, text)


@pytest.mark.parametrize(
    "query,text",
    [
        ("VR 1000631423", "Meta Quest VR-Brille, Bestellung 1000631424"),
        ("KA-ZY 244", "Rechnung über 244 EUR"),  # the bare number is too weak alone
        ("DE89 3704 0044 0532 0130 00", "IBAN DE89 3704 0044 0532 0130 01"),
    ],
)
def test_identifier_does_not_match_other_text(query, text):
    assert not _matches(query, text)


def test_prose_words_are_case_insensitive_and_or_combined():
    assert _matches("Sondertilgung Vorfälligkeit", "die SONDERTILGUNG ist einmal jährlich")
    assert _matches("Kfz-Haftpflicht", "Ihre KFZ Haftpflicht")
    assert _matches("Kfz-Haftpflicht", "kfzhaftpflicht")
    assert not _matches("Sondertilgung", "Tilgungsplan")


def test_prose_around_an_identifier_is_not_searched():
    # 'Rechnung' alone would match half the archive and bury the identifier hit.
    assert not _matches("Rechnung VR 1000631423", "Rechnung Strom 2025")


def test_runs_and_variants():
    assert _code_runs("Rechnung VR 1000631423") == [["VR", "1000631423"]]
    assert _code_runs("MAU044 GRG 915 / 2021") == [["MAU044", "GRG", "915", "/", "2021"]]
    assert _code_runs("Sondertilgung vorzeitig") == []
    v = literal_variants("Rechnung Nr. 00206538856/25/03")
    assert "Nr00206538856/25/03" in v and "00206538856/25/03" in v
    assert literal_variants("KA ZY 244") == ["KAZY244", "ZY244"]  # never bare '244'


def test_too_short_text_gives_no_filter():
    assert _build_where_document("a") is None
    assert _build_where_document("OR") is None


# ── visibility before the cut ────────────────────────────────────────────────


class _FakeChroma:
    def __init__(self):
        self.where = "unset"

    async def query(self, query_texts, n_results, where=None, where_document=None, where_pending=None):
        if where_pending is not None:
            where = await where_pending
        self.where = where
        if where and where["paperless_id"]["$in"] == [-1]:
            return [[]]
        return [[{"metadata": {"paperless_id": 2, "page_number": 1}, "distance": 0.1, "document": "x"}]]


def _FakeDoc(i):
    from models.paperless_document import PaperlessDocument

    return PaperlessDocument.model_construct(id=i, tags=[])


class _FakePl:
    def __init__(self, ids):
        self._ids = ids

    async def visible_ids(self):
        return set(self._ids)

    async def list_documents(self, ids=None, **kw):
        return [_FakeDoc(i) for i in (ids or []) if i in self._ids]

    async def get_tag_map(self):
        return {}


def test_search_filters_visibility_before_ranking(monkeypatch):
    import pipelines.search as ps

    fake = _FakeChroma()
    monkeypatch.setattr(ps, "chroma", fake)
    monkeypatch.setattr(ps.sidecar_service, "has_actions", lambda _i: False)
    filters = {"query": None, "correspondent": None, "document_type": None,
               "tags": None, "created": None, "added": None}
    res = asyncio.run(ps.search(filters, semantic_query="x", paperless_client=_FakePl([2, 5, 3])))
    assert fake.where == {"paperless_id": {"$in": [2, 3, 5]}}
    assert [r.document.id for r in res] == [2]


def test_search_with_nothing_visible_finds_nothing(monkeypatch):
    import pipelines.search as ps

    fake = _FakeChroma()
    monkeypatch.setattr(ps, "chroma", fake)
    filters = {"query": None, "correspondent": None, "document_type": None,
               "tags": None, "created": None, "added": None}
    assert asyncio.run(ps.search(filters, semantic_query="x", paperless_client=_FakePl([]))) == []
    assert fake.where == {"paperless_id": {"$in": [-1]}}  # matches no document


def test_visible_ids_serves_stale_and_refreshes(monkeypatch):
    import services.paperless as sp

    calls = []

    async def fake_refresh(self, key):
        calls.append(key)
        sp._VISIBLE_IDS[key] = (sp.time.monotonic(), {len(calls)})
        sp._IDS_REFRESHING.discard(key)
        return {len(calls)}

    monkeypatch.setattr(sp.PaperlessClient, "_refresh_visible_ids", fake_refresh)
    sp._VISIBLE_IDS.clear()
    pl = sp.PaperlessClient("http://x", "tok")

    async def go():
        first = await pl.visible_ids()          # nothing cached: waits for the fetch
        again = await pl.visible_ids()          # fresh: no fetch
        key = ("http://x", "Token tok")
        sp._VISIBLE_IDS[key] = (sp.time.monotonic() - 10_000, first)
        stale = await pl.visible_ids()          # stale: old ids now, refresh behind
        await asyncio.sleep(0)
        return first, again, stale

    first, again, stale = asyncio.run(go())
    assert first == again == stale == {1}
    assert len(calls) == 2

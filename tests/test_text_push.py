"""Which documents the text write-back considers changed.

The selection is the whole safety story of this feature: every ID it returns
gets its Paperless OCR text overwritten irreversibly. Two failure modes matter
and both are cheap to encode — pushing text that is merely wrapped differently
(would rewrite the entire archive on every sync) and pushing a failed
extraction over a working OCR result.
"""

import json

import pytest

from pipelines.text_push import MIN_TEXT_LENGTH, find_text_diffs
from services.sidecar_service import SidecarService

_GOOD = "Invoice 2026-0815 from Musterfirma GmbH, total 1.234,56 EUR, due 30.06.2026."


@pytest.fixture
def sidecars(tmp_path):
    service = SidecarService(str(tmp_path))

    def _write(doc_id: int, full_text: str) -> None:
        (tmp_path / f"{doc_id}.json").write_text(
            json.dumps({"paperless_id": doc_id, "full_text": full_text})
        )

    service.write = _write  # test-only helper, not part of the service API
    return service


def _docs(*pairs) -> list[dict]:
    return [{"id": doc_id, "content": content} for doc_id, content in pairs]


def test_identical_text_is_not_pushed(sidecars):
    sidecars.write(1, _GOOD)
    assert find_text_diffs(_docs((1, _GOOD)), sidecars) == []


def test_differing_text_is_pushed(sidecars):
    sidecars.write(1, _GOOD)
    assert find_text_diffs(_docs((1, "lnvoíce 2O26-O815 frorn Musterfirrna")), sidecars) == [1]


@pytest.mark.parametrize(
    "paperless_content",
    [
        _GOOD.replace(" ", "\n"),          # different line wrapping
        f"  {_GOOD}  ",                    # leading/trailing whitespace
        _GOOD.replace(", ", ",   "),       # collapsed runs of spaces
        _GOOD.replace(" ", "\t"),          # tabs instead of spaces
    ],
)
def test_whitespace_only_differences_are_not_pushed(sidecars, paperless_content):
    """Otherwise every sync would rewrite every document in the archive."""
    sidecars.write(1, _GOOD)
    assert find_text_diffs(_docs((1, paperless_content)), sidecars) == []


def test_short_vision_text_never_overwrites_paperless(sidecars):
    """A near-empty extraction means the vision run failed, not that the page is empty."""
    sidecars.write(1, "x" * (MIN_TEXT_LENGTH - 1))
    assert find_text_diffs(_docs((1, "a real OCR result with actual content")), sidecars) == []


def test_documents_without_a_sidecar_are_skipped(sidecars):
    """Not ingested yet — there is nothing of ours to push."""
    assert find_text_diffs(_docs((99, "whatever Paperless has")), sidecars) == []


def test_empty_paperless_content_counts_as_a_difference(sidecars):
    """The common case: Paperless failed to OCR the scan at all."""
    sidecars.write(1, _GOOD)
    assert find_text_diffs(_docs((1, "")), sidecars) == [1]
    assert find_text_diffs([{"id": 1, "content": None}], sidecars) == [1]


def test_only_changed_documents_are_returned(sidecars):
    sidecars.write(1, _GOOD)
    sidecars.write(2, _GOOD)
    sidecars.write(3, "Another document entirely, long enough to pass the floor.")
    docs = _docs((1, _GOOD), (2, "garbled"), (3, ""))
    assert find_text_diffs(docs, sidecars) == [2, 3]

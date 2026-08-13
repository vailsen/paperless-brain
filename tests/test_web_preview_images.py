"""Preview images for web results.

SearXNG passes `img_src` through only when the answering engine supplies one,
which is why the result list used to be sometimes-all, sometimes-none. Every
card must end up with a picture or the same placeholder — never a gap.
"""

import asyncio

import pytest

from models.web_result import WebSearchResult
from services import chat_service as C


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clear_cache():
    C._OG_IMAGE_CACHE.clear()
    yield
    C._OG_IMAGE_CACHE.clear()


@pytest.fixture
def og(monkeypatch):
    """Stub the per-page fetch; record which URLs were asked for."""
    asked: list[str] = []
    images = {}

    async def fake(url: str) -> str:
        asked.append(url)
        return images.get(url, "")

    monkeypatch.setattr(C, "_og_image", fake)
    return asked, images


def _result(url: str, img: str = "") -> WebSearchResult:
    return WebSearchResult(title="t", url=url, img_src=img)


def test_missing_images_are_filled_in(og):
    asked, images = og
    images["https://a.test/1"] = "https://a.test/pic.jpg"
    results = [_result("https://a.test/1")]

    _run(C._fill_preview_images(results))
    assert results[0].img_src == "https://a.test/pic.jpg"


def test_existing_images_are_left_alone(og):
    asked, _ = og
    results = [_result("https://a.test/1", img="https://engine.test/thumb.jpg")]

    _run(C._fill_preview_images(results))
    assert results[0].img_src == "https://engine.test/thumb.jpg"
    assert asked == []


def test_only_the_top_n_are_fetched(og):
    """Every result would mean N requests to third-party sites per search."""
    asked, _ = og
    results = [_result(f"https://a.test/{i}") for i in range(12)]

    _run(C._fill_preview_images(results))
    assert len(asked) == C._OG_IMAGE_TOP_N


def test_a_page_without_an_image_stays_empty(og):
    results = [_result("https://a.test/1")]
    _run(C._fill_preview_images(results))
    assert results[0].img_src == ""


def test_one_failing_fetch_does_not_sink_the_others(monkeypatch):
    async def fake(url: str) -> str:
        if "bad" in url:
            raise RuntimeError("connection reset")
        return "https://ok.test/pic.jpg"

    monkeypatch.setattr(C, "_og_image", fake)
    results = [_result("https://bad.test/1"), _result("https://good.test/2")]

    _run(C._fill_preview_images(results))
    assert results[0].img_src == ""
    assert results[1].img_src == "https://ok.test/pic.jpg"


def test_no_results_is_not_an_error(og):
    _run(C._fill_preview_images([]))


# ── Tag parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "html,expected",
    [
        ('<meta property="og:image" content="https://x.test/a.jpg">', "https://x.test/a.jpg"),
        # Attribute order is not fixed in the wild.
        ('<meta content="https://x.test/b.jpg" property="og:image">', "https://x.test/b.jpg"),
        ('<meta name="twitter:image" content="https://x.test/c.jpg">', "https://x.test/c.jpg"),
        ("<meta property='og:image' content='https://x.test/d.jpg'>", "https://x.test/d.jpg"),
        ('<meta property="og:image:secure_url" content="https://x.test/e.jpg">', "https://x.test/e.jpg"),
        ("<html><head><title>no image</title></head>", None),
    ],
)
def test_og_image_tag_variants(html, expected):
    m = C._OG_IMAGE_RE.search(html) or C._OG_IMAGE_RE_REVERSED.search(html)
    assert (m.group(1) if m else None) == expected


def test_relative_image_url_is_resolved_against_the_page():
    """A bare '/img/a.jpg' in a src attribute renders as nothing."""
    import urllib.parse

    m = C._OG_IMAGE_RE.search('<meta property="og:image" content="/img/a.jpg">')
    assert urllib.parse.urljoin("https://shop.test/p/123", m.group(1)) == (
        "https://shop.test/img/a.jpg"
    )

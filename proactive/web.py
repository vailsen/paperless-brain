"""Web access for proactive jobs in the worker: SearXNG search and page fetch.

The worker does not load the chat tools (they need Chroma), so this is a small
layer of its own with two rules built in:

- every query passes `guard.check` first — a rejected query never leaves the
  machine, the caller gets the tool-error text back;
- every page that was actually fetched is recorded in the `WebRun`, because a
  recommendation may only cite a URL fetched in the same run (architecture §10)
  — a URL the model remembers is not a source.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from proactive import clock, guard

log = logging.getLogger("proactive.web")

MAX_PAGE_CHARS = 8000


class QueryRejected(Exception):
    """The guard refused the query; `str(exc)` is the text for the model."""


@dataclass
class WebRun:
    names: list[str] = field(default_factory=list)
    fetched: dict[str, str] = field(default_factory=dict)  # url → retrieval date
    queries: list[str] = field(default_factory=list)

    async def search(self, query: str, *, max_results: int = 5, language: str = "") -> list[dict]:
        rejected = guard.check(query, self.names)
        if rejected:
            raise QueryRejected(rejected)
        self.queries.append(query)
        return await _searx(query, max_results=max_results, language=language)

    async def fetch(self, url: str) -> str | None:
        if not url.startswith(("http://", "https://")):
            return None
        text = await _fetch(url)
        if text:
            self.fetched[url] = clock.local_now().date().isoformat()
            return text[:MAX_PAGE_CHARS]
        return None


async def _searx(query: str, *, max_results: int, language: str) -> list[dict]:
    from config.settings import settings

    form = {"q": query, "format": "json"}
    if language:
        form["language"] = language
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{settings.searxng_host.rstrip('/')}/search", data=form)
        r.raise_for_status()
        data = r.json()
    return [
        {"title": h.get("title", ""), "url": h.get("url", ""), "snippet": h.get("content", "")}
        for h in (data.get("results") or [])[:max_results]
        if h.get("url")
    ]


async def _fetch(url: str) -> str | None:
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return None
    try:
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        if not downloaded:
            return None
        return await asyncio.to_thread(
            lambda: trafilatura.extract(downloaded, include_links=False, include_images=False)
        )
    except Exception as exc:
        log.info("fetch failed for a page: %s", type(exc).__name__)
        return None

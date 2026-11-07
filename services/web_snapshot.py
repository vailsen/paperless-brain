"""A web source as a picture, the cited passage marked — like a document page.

A chat answer that verified something on the web cites the page as a link
(`services/web_citations.py`). Tapped, it opens in the Source tab like a
document: `snapshot(url, claim)` finds the passage on the page that best
supports the claim (`citations.best_passage`, the same scoring as for
documents), loads the page in headless Chromium (crawl4ai), marks the passage
(CSS Custom Highlight + an outline), scrolls it into view and takes a
screenshot of the viewport. The picture is cached under `data/web_shots/`.

Limits, by design:
- crawl4ai is the `[crawl]` extra; without it there is no picture (`None`)
  and the link stays a link.
- A passage the script cannot find on the rendered page (a bot wall, text in
  an iframe, a cookie wall) gives a screenshot without a mark — `marked`
  says so; a wrong mark would be worse than none.
- Only a public http(s) address is fetched: the click comes from the browser,
  so the URL is checked here (`_public`) — no request to the LAN.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("web_snapshot")

VIEWPORT = (1280, 900)
TIMEOUT_S = 75
_locks: dict[str, asyncio.Lock] = {}


def shot_dir() -> Path:
    from config.settings import settings

    d = Path(settings.app_path) / "data" / "web_shots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(url: str, claim: str) -> str:
    return hashlib.sha256(f"{url}\n{' '.join(claim.split())}".encode()).hexdigest()[:32]


def valid_key(key: str) -> bool:
    return len(key) == 32 and all(c in "0123456789abcdef" for c in key)


async def _public(url: str) -> bool:
    """http(s) to a host that resolves to public addresses only."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for *_rest, addr in infos:
        ip = ipaddress.ip_address(addr[0])
        if not ip.is_global:
            return False
    return bool(infos)


# Marks the passage (window.__pbNeedles, longest first) and scrolls it into
# view. Text nodes are joined with whitespace folded, so a sentence split over
# <a>, <em> or a line break is still found.
_MARK_JS = r"""
(() => {
  // Compared without any whitespace: inline elements split a sentence into
  // text nodes ("muscle." + "[2]"), and the extractor and the page differ in
  // spacing; joining nodes with a space lost the match.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.parentElement && !/^(SCRIPT|STYLE|NOSCRIPT)$/.test(n.parentElement.tagName))
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT });
  let folded = ''; const map = [];  // folded index -> [node, offset]
  for (let n; (n = walker.nextNode());) {
    const v = n.nodeValue;
    for (let i = 0; i < v.length; i++) if (!/\s/.test(v[i])) { folded += v[i].toLowerCase(); map.push([n, i]); }
  }
  for (const needle of (window.__pbNeedles || [])) {
    const f = needle.replace(/\s+/g, '').toLowerCase();
    const i = f ? folded.indexOf(f) : -1;
    if (i < 0) continue;
    const [a, ao] = map[i]; const [b, bo] = map[i + f.length - 1];
    const range = document.createRange();
    range.setStart(a, ao); range.setEnd(b, bo + 1);
    if (window.CSS && CSS.highlights) {
      const st = document.createElement('style');
      st.textContent = '::highlight(pb-mark){background:#ffe066;color:inherit}';
      document.head.appendChild(st);
      CSS.highlights.set('pb-mark', new Highlight(range));
    }
    const r = range.getBoundingClientRect();
    window.scrollTo(0, Math.max(0, window.scrollY + r.top - innerHeight / 3));
    const r2 = range.getBoundingClientRect();
    const box = document.createElement('div');
    box.style.cssText = `position:fixed;left:${r2.left - 6}px;top:${r2.top - 4}px;width:${r2.width + 12}px;` +
      `height:${r2.height + 8}px;border:3px solid #f59f00;border-radius:4px;z-index:2147483647;pointer-events:none`;
    document.body.appendChild(box);
    document.documentElement.setAttribute('data-pb-marked', '1');
    return true;
  }
  document.documentElement.setAttribute('data-pb-marked', '0');
  return false;
})();
"""


def _needles(passage: str) -> list[str]:
    """What to look for on the rendered page: the passage, then shorter
    pieces of it — rendered text differs from extracted text at the edges."""
    p = " ".join(passage.split())
    out = [p]
    import re

    for seg in sorted((s.strip() for s in re.split(r"(?<=[.!?])\s+", p)), key=len, reverse=True):
        if len(seg) >= 25:
            out.append(seg)
            words = seg.split()
            if len(words) > 8:
                out.append(" ".join(words[:8]))
    seen, uniq = set(), []
    for n in out:
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


async def _page_text(url: str) -> str:
    from services import chat_service

    text = await chat_service._trafilatura_fetch(url)
    if not text or chat_service._unreadable_page(text):
        text = await chat_service._crawl4ai_fetch(url) or text
    return text or ""


async def _screenshot(url: str, needles: list[str]) -> tuple[bytes | None, bool]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    browser = BrowserConfig(headless=True, java_script_enabled=True, user_agent_mode="random",
                            viewport_width=VIEWPORT[0], viewport_height=VIEWPORT[1])
    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, wait_until="domcontentloaded", page_timeout=45_000,
        delay_before_return_html=1.0, magic=True, simulate_user=True, override_navigator=True,
        js_code=["window.__pbNeedles = " + json.dumps(needles) + ";", _MARK_JS],
        screenshot=True, force_viewport_screenshot=True, screenshot_wait_for=0.8,
    )
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url=url, config=run)
    shot = getattr(result, "screenshot", None)
    html = getattr(result, "html", "") or ""
    marked = 'data-pb-marked="1"' in html
    if not shot:
        return None, False
    return base64.b64decode(shot), marked


async def snapshot(url: str, claim: str) -> dict | None:
    """`{key, url, title, passage, marked}` — the picture is at `shot_dir()/key.png`;
    None when the page cannot be pictured (no crawl4ai, not public, failed)."""
    if not await _public(url):
        log.info("web snapshot refused: %s is not a public address", url)
        return None
    key = _key(url, claim)
    meta_path = shot_dir() / f"{key}.json"
    async with _locks.setdefault(key, asyncio.Lock()):
        if meta_path.exists() and (shot_dir() / f"{key}.png").exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        try:
            import crawl4ai  # noqa: F401
        except ImportError:
            return None
        from services.citations import best_passage

        try:
            text = await asyncio.wait_for(_page_text(url), timeout=TIMEOUT_S)
            passage, _score = best_passage(text, claim) if text and claim else ("", 0.0)
            png, marked = await asyncio.wait_for(_screenshot(url, _needles(passage) if passage else []),
                                                 timeout=TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — a page that cannot be pictured stays a link
            log.info("web snapshot of %s failed: %s", url, type(exc).__name__)
            return None
        if not png:
            return None
        (shot_dir() / f"{key}.png").write_bytes(png)
        meta = {"key": key, "url": url, "title": urlparse(url).hostname or url,
                "passage": passage, "marked": bool(marked and passage)}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        _prune()
        return meta


#: The cache keeps the newest pictures; a page asked for again is taken anew.
KEEP = 300


def _prune() -> None:
    try:
        shots = sorted(shot_dir().glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in shots[KEEP:]:
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)
    except OSError:
        pass

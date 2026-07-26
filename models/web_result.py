# models/web_result.py
from dataclasses import dataclass


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""     # SearXNG content preview
    published: str = ""   # YYYY-MM-DD (if provided)
    img_src: str = ""     # thumbnail/image URL (if provided)
    engine: str = ""      # source engine (e.g. "google", "duckduckgo")

# services/vision.py

import base64
import json
import re
from abc import ABC, abstractmethod

import httpx

from config.extraction_rules import (
    CONDENSED_SUMMARY_PROMPT,
    EXTRACTION_JSON_SCHEMA,
    TABLE_CONTINUATION_CONTEXT,
    get_extraction_rules,
)
from services.extraction_guard import (
    MAX_PLAUSIBLE_PAGE_WORDS,
    inspect_page_text,
    salvage_page_text,
)
from services.ollama_watchdog import touch as _wol_touch
from models.extraction import ExtractedContent, PageImage

# Cyrillic, CJK, Hiragana/Katakana, Hangul, Greek — local models occasionally
# drift into another script mid-summary; none belong in a Latin-script summary.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[Ͱ-ϿЀ-ӿ぀-ヿ一-鿿가-힯]"
)


def _archive_language_name() -> str:
    from i18n import language_name
    from werkbank.settings_store import get_archive_language
    return language_name(get_archive_language())


class VisionClient(ABC):
    """Common interface — pipeline only knows about this."""

    @abstractmethod
    async def analyze_document(
        self,
        page: PageImage,
        document_type: str,
        ai_doc_type: str,
        prev_table: dict | None = None,
    ) -> ExtractedContent: ...

    @abstractmethod
    async def summarize_document(self, full_summary: str) -> str: ...


class OllamaVisionClient(VisionClient):
    def __init__(self, base_url: str = "", model: str = ""):
        self._base_url = base_url  # explicit override for scripts; empty = read from settings_store
        self._model = model

    @property
    def base_url(self) -> str:
        if self._base_url:
            return self._base_url
        from werkbank.settings_store import get_ingest_server
        return get_ingest_server()

    @property
    def model(self) -> str:
        if self._model:
            return self._model
        from werkbank.settings_store import get_ingest_model
        return get_ingest_model()

    def _oai_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"

    def _chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/chat"

    async def analyze_document(
        self,
        page: PageImage,
        document_type: str,
        ai_doc_type: str,
        prev_table: dict | None = None,
    ) -> ExtractedContent:
        # Resolved per call: the profile is settable in Settings > Processing,
        # so binding the rules once at import would serve stale ones until restart.
        _rules_map = get_extraction_rules()
        rules = _rules_map.get(document_type, _rules_map["_default"])
        prompt = rules["prompt"] + (
            f"\n\nWrite page_summary and image descriptions in {_archive_language_name()}. "
            "page_text is extracted verbatim and keeps the document's original language."
        )
        if prev_table and prev_table.get("rows"):
            prompt += TABLE_CONTINUATION_CONTEXT.format(
                caption=prev_table.get("caption", ""),
                columns=list(prev_table["rows"][-1].keys()),
            )
        result = await self._extract_once(page, prompt, ai_doc_type)

        # Guard against repetition loops. The sampling settings below keep
        # presence_penalty at 0 on purpose, which makes loops more likely; a
        # loop that reaches the sidecar is invisible afterwards, so catch it
        # here. Loops are sampling-dependent, so one retry usually suffices.
        verdict = inspect_page_text(result.page_text)
        if not verdict:
            print(
                f"[vision] page {page.page_number}: degenerate extraction "
                f"({verdict.reason}) — retrying once",
                flush=True,
            )
            result = await self._extract_once(page, prompt, ai_doc_type)
            retry_verdict = inspect_page_text(result.page_text)
            if not retry_verdict:
                print(
                    f"[vision] page {page.page_number}: still degenerate after "
                    f"retry ({retry_verdict.reason}) — truncating to the first "
                    f"{MAX_PLAUSIBLE_PAGE_WORDS} words",
                    flush=True,
                )
                result.page_text = salvage_page_text(result.page_text)
        return result

    async def _extract_once(
        self, page: PageImage, prompt: str, ai_doc_type: str
    ) -> ExtractedContent:
        """One vision call for one page. Retried by analyze_document if degenerate."""
        image_b64 = base64.b64encode(page.image_bytes).decode("utf-8")
        _wol_touch()
        # Native Ollama endpoint: "format" constrains decoding to the JSON schema —
        # dense pages otherwise produce unparseable free-form JSON.
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._chat_url(),
                json={
                    "model": self.model,
                    "messages": [{
                        "role": "user",
                        "content": prompt,
                        "images": [image_b64],
                    }],
                    "format": EXTRACTION_JSON_SCHEMA,
                    # Qwen3 degrades under greedy decoding (temp 0 → skipped page
                    # regions); use the model-card sampling. presence_penalty must be
                    # 0 — the modelfile ships 1.5, which makes the model drop
                    # repetitive blocks (table rows, repeated JSON keys).
                    "options": {
                        "temperature": 0.7,
                        "top_p": 0.8,
                        "top_k": 20,
                        "presence_penalty": 0,
                    },
                    "stream": False,
                    "think": False,
                },
                timeout=600,
            )
            response.raise_for_status()
        raw_text = response.json()["message"]["content"]
        return self._parse_fields(raw_text, page, ai_doc_type)

    async def describe_page(self, page: PageImage, question: str) -> str:
        """Free-form visual analysis of a single page with a user-defined question."""
        image_b64 = base64.b64encode(page.image_bytes).decode("utf-8")
        prompt = (
            f"You are analyzing page {page.page_number} of {page.total_pages} "
            "of a scanned document.\n\n"
            f"Task: {question}\n\n"
            "Answer precisely. If the task cannot be answered directly, "
            "describe the relevant visible content of the page."
        )
        _wol_touch()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._oai_url(),
                json={
                    "model": self.model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                            {"type": "text", "text": prompt},
                        ],
                    }],
                    "stream": False,
                    "think": False,
                },
                timeout=300,
            )
            response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    async def summarize_document(self, full_summary: str) -> str:
        prompt = (
            CONDENSED_SUMMARY_PROMPT.format(language=_archive_language_name())
            + "\n\nPage summaries:\n" + full_summary
        )
        # Local models occasionally slip into another script mid-sentence
        # (e.g. Russian words in a German summary) — retry once, then drop the
        # condensed summary so consumers fall back to full_summary.
        for _attempt in range(2):
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._oai_url(),
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "think": False,
                    },
                    timeout=120,
                )
                response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"].strip()
            if not _FOREIGN_SCRIPT_RE.search(result):
                return result
        return ""

    def _parse_fields(
        self, raw_text: str, page: PageImage, ai_doc_type: str
    ) -> ExtractedContent:
        cleaned = re.sub(r"^```[^\n]*\n?|\n?```[^\n]*$", "", raw_text.strip())
        parsed = json.loads(cleaned)
        doc_type = ai_doc_type if ai_doc_type else (parsed.get("document_type") or "")
        return ExtractedContent(
            page=page.page_number,
            page_text=parsed.get("page_text", ""),
            tables=parsed.get("tables", []),
            actions=parsed.get("actions", []),
            page_summary=parsed.get("page_summary", ""),
            cross_references=parsed.get("cross_references", []),
            document_type=doc_type,
        )

# services/vision.py

"""Vision backends for document ingestion.

One page in, one ExtractedContent out. Three transports are supported and the
model is chosen in Settings > Processing from the same registry the chat uses:

    OllamaVisionClient           native /api/chat, `format` constrains decoding
    OpenAICompatibleVisionClient /v1/chat/completions, response_format json_schema
    AnthropicVisionClient        /v1/messages, a tool schema constrains the output

Everything above the transport — prompt assembly, the degenerate-output retry,
JSON parsing — lives in the base class. A new provider therefore cannot skip the
extraction guard by forgetting to call it, which is the failure that would be
invisible afterwards: a bad page reaches the sidecar and nothing ever says so.

All three force the model to emit the extraction schema rather than free-form
JSON. Dense pages otherwise produce output that will not parse, and the pipeline
has no way to recover a page it could not read.
"""

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

_SCHEMA_TOOL_NAME = "emit_page_extraction"


def _archive_language_name() -> str:
    from i18n import language_name
    from werkbank.settings_store import get_archive_language
    return language_name(get_archive_language())


class VisionClient(ABC):
    """Common interface — the pipeline only knows about this."""

    # ── Transport primitives ─────────────────────────────────────────────────

    @abstractmethod
    async def _vision_json(self, prompt: str, image_b64: str) -> str:
        """Page + prompt in, a JSON string matching EXTRACTION_JSON_SCHEMA out."""

    @abstractmethod
    async def _vision_text(self, prompt: str, image_b64: str) -> str:
        """Page + prompt in, free-form text out."""

    @abstractmethod
    async def _text(self, prompt: str) -> str:
        """Prompt in, free-form text out. No image."""

    # ── Shared behaviour ─────────────────────────────────────────────────────

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
        raw_text = await self._vision_json(prompt, image_b64)
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
        return (await self._vision_text(prompt, image_b64)).strip()

    async def summarize_document(self, full_summary: str) -> str:
        prompt = (
            CONDENSED_SUMMARY_PROMPT.format(language=_archive_language_name())
            + "\n\nPage summaries:\n" + full_summary
        )
        # Local models occasionally slip into another script mid-sentence
        # (e.g. Russian words in a German summary) — retry once, then drop the
        # condensed summary so consumers fall back to full_summary.
        for _attempt in range(2):
            _wol_touch()
            result = (await self._text(prompt)).strip()
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


class OllamaVisionClient(VisionClient):
    """Native Ollama. `format` constrains decoding to the extraction schema."""

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

    async def _vision_json(self, prompt: str, image_b64: str) -> str:
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
        return response.json()["message"]["content"]

    async def _vision_text(self, prompt: str, image_b64: str) -> str:
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
        return response.json()["choices"][0]["message"]["content"]

    async def _text(self, prompt: str) -> str:
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
        return response.json()["choices"][0]["message"]["content"]


class OpenAICompatibleVisionClient(VisionClient):
    """Any /v1/chat/completions endpoint: OpenAI, MiniMax, vLLM, LM Studio, …"""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        # base_url is stored with the /v1 suffix, exactly as the chat registry
        # keeps it, so the same registry entry works for both.
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key

    def _headers(self) -> dict:
        # Local servers accept and ignore a bearer header; sending it
        # unconditionally keeps one code path.
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _url(self) -> str:
        base = self._base_url
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    async def _post(self, payload: dict, timeout: int) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._url(), json=payload, headers=self._headers(), timeout=timeout
            )
            response.raise_for_status()
        return response.json()

    async def _vision_json(self, prompt: str, image_b64: str) -> str:
        # strict=False on purpose: strict mode demands additionalProperties:false
        # and every property required throughout, which the table rows ("any
        # object") cannot satisfy. Non-strict still pins the shape well enough,
        # and _parse_fields tolerates a fenced ```json wrapper from servers that
        # ignore response_format entirely.
        def _payload(text: str, with_schema: bool) -> dict:
            body = {
                "model": self.model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                        {"type": "text", "text": text},
                    ],
                }],
                "stream": False,
            }
            if with_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "page_extraction",
                        "schema": EXTRACTION_JSON_SCHEMA,
                        "strict": False,
                    },
                }
            return body

        try:
            data = await self._post(_payload(prompt, True), timeout=600)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            # Plenty of OpenAI-compatible servers reject response_format wholesale.
            # Fall back to asking for JSON in the prompt rather than failing the
            # page — the parser strips code fences either way. Built fresh rather
            # than mutating the first payload, so the retry shares no state.
            data = await self._post(
                _payload(
                    prompt + "\n\nRespond with JSON only, no prose and no code fence.",
                    False,
                ),
                timeout=600,
            )
        return data["choices"][0]["message"]["content"]

    async def _vision_text(self, prompt: str, image_b64: str) -> str:
        data = await self._post(
            {
                "model": self.model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "stream": False,
            },
            timeout=300,
        )
        return data["choices"][0]["message"]["content"]

    async def _text(self, prompt: str) -> str:
        data = await self._post(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=120,
        )
        return data["choices"][0]["message"]["content"]


class AnthropicVisionClient(VisionClient):
    """Claude via /v1/messages. A tool schema takes the place of `format`."""

    _API = "https://api.anthropic.com/v1/messages"
    _VERSION = "2023-06-01"

    def __init__(self, model: str, api_key: str, base_url: str = ""):
        self.model = model
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")

    def _url(self) -> str:
        return f"{self._base_url}/v1/messages" if self._base_url else self._API

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._VERSION,
            "content-type": "application/json",
        }

    @staticmethod
    def _image_block(image_b64: str) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",  # PdfExtractor renders JPEG
                "data": image_b64,
            },
        }

    async def _post(self, payload: dict, timeout: int) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._url(), json=payload, headers=self._headers(), timeout=timeout
            )
            response.raise_for_status()
        return response.json()

    async def _vision_json(self, prompt: str, image_b64: str) -> str:
        # tool_choice forces the model through the schema — the Anthropic
        # equivalent of Ollama's `format`, and far more reliable than asking for
        # JSON in the prompt on a dense page.
        data = await self._post(
            {
                "model": self.model,
                "max_tokens": 8192,
                "tools": [{
                    "name": _SCHEMA_TOOL_NAME,
                    "description": "Return the structured extraction for this page.",
                    "input_schema": EXTRACTION_JSON_SCHEMA,
                }],
                "tool_choice": {"type": "tool", "name": _SCHEMA_TOOL_NAME},
                "messages": [{
                    "role": "user",
                    "content": [self._image_block(image_b64), {"type": "text", "text": prompt}],
                }],
            },
            timeout=600,
        )
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return json.dumps(block.get("input") or {})
        # Refusals and max_tokens stops arrive as plain text; let the parser
        # raise so analyze_document's retry gets a chance.
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )

    async def _vision_text(self, prompt: str, image_b64: str) -> str:
        data = await self._post(
            {
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{
                    "role": "user",
                    "content": [self._image_block(image_b64), {"type": "text", "text": prompt}],
                }],
            },
            timeout=300,
        )
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )

    async def _text(self, prompt: str) -> str:
        data = await self._post(
            {
                "model": self.model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )


def build_vision_client(username: str = "", token: str = "") -> VisionClient:
    """The vision client for the model chosen in Settings > Processing.

    The ingestion model is stored as a registry entry name, and the registry is
    per user and encrypted with that user's Paperless token — so the key can only
    be read while that user is signed in. Sync is always triggered from a signed-in
    session, and the credentials of whoever triggers it are used.

    Falls back to the Ollama client (settings_store server/model, then .env) when
    there is no user context, no stored name, or the name no longer resolves —
    that is the pre-registry behaviour and it keeps working untouched.
    """
    from werkbank.settings_store import get_ingest_model_name

    name = get_ingest_model_name()
    if not name or not (username and token):
        return OllamaVisionClient()

    try:
        from services.model_registry import get_by_name

        entry = get_by_name(name, username, token)
    except Exception:
        entry = None
    if not entry:
        return OllamaVisionClient()

    model = entry.get("model") or ""
    base_url = (entry.get("base_url") or "").rstrip("/")
    api_key = entry.get("api_key") or ""

    if entry.get("backend") == "anthropic":
        if not api_key:
            from config.settings import settings
            api_key = settings.anthropic_api_key  # same global fallback the chat uses
        return AnthropicVisionClient(model=model, api_key=api_key, base_url=base_url)

    if not base_url:
        return OllamaVisionClient()
    return OpenAICompatibleVisionClient(base_url=base_url, model=model, api_key=api_key)

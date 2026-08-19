"""Werkbank v2 — the tool layer, and the only place `trust` is decided.

Why this exists instead of calling `services.chat_service.execute_tool()`
directly: that function returns a string *formatted for a model*. D2 compares a
quote against the text a tool actually returned, and after formatting there is
no such text any more. So every call goes through here, and every call leaves a
record:

    source id · tool · args · raw text · trust · ref · hits · retrieved_at

Three rules live here rather than in a prompt, because a prompt is a request
and this has to be a guarantee:

- **`trust` comes from the tool.** The model may write whatever it likes into
  `source.trust`; it is overwritten from this log afterwards.
- **`sources_restrict` is enforced before the call**, not judged after it. A
  subtask restricted to Paperless cannot reach a personal note at all.
- **A document search also searches the vault**, unless restricted. That is
  where contradictions between a filed document and a remembered guess come
  from, and they only surface if both are looked at.
"""

from __future__ import annotations

import json

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from werkbank.v2.registry import Registry

_log = logging.getLogger(__name__)

# Which store a tool reads. Drives `sources_restrict` (D7) and the source type
# recorded on every fact.
TOOL_SOURCE_TYPE: dict[str, str] = {
    "search": "paperless",
    "search_exact": "paperless",
    "get_document_details": "paperless",
    "get_document_page_text": "paperless",
    "get_document_table": "paperless",
    "get_actions": "paperless",
    "vault_search": "vault",
    "search_memory": "note",
    "search_emails": "email",
    "search_calendar": "calendar",
    "web_search": "web",
    "web_fetch_page": "web",
    "calculate": "computed",
    "get_current_date": "computed",
}

# A document search fires these as well, so a filed document and a personal
# note about the same thing land in the same subtask.
VAULT_COMPANION_FOR = {"search", "search_exact"}

# Search tools return a ranked list; a quote from that list is a quote from a
# snippet. For the web that is forbidden outright (§6.2) — the snippet shows
# the name of a thing, never what it is for, which is exactly how a competitor
# search returns products with the wrong purpose.
SNIPPET_ONLY_TOOLS = {"web_search"}


@dataclass
class ToolCallRecord:
    source_id: str
    tool: str
    args: dict
    raw_text: str
    trust: str
    source_type: str
    ref: str = ""
    hits: int | None = None
    retrieved_at: str = ""
    quotable: bool = True
    outcome: str = "ok"


# Tools that retrieve nothing — they compute or report. An agent whose whole
# tool set is these (the synthesizer, the contradiction checker) is *meant* to
# work from inherited facts, so "no tool was called" is not evidence that it
# answered from memory.
UTILITY_TOOLS = frozenset({"calculate", "get_current_date"})


def has_retrieval(tools: list[str] | set[str]) -> bool:
    return bool(set(tools) - UTILITY_TOOLS)


# ── What a tool call actually did ────────────────────────────────────────────
#
# Three outcomes, and the difference between them is the difference between a
# finding and a false negative:
#
#   ok      — it ran and returned something
#   empty   — it ran and there was nothing. This *is* evidence of absence.
#   failed  — it never ran, or could not read what it reached (no credentials,
#             a search host refusing, a paywall stub, an exception). This is
#             evidence of nothing at all.
#
# The tools state their outcome in prose, for a model to read. Guessing at that
# prose is not a nice property, and the honest fix is for `execute_tool()` to
# return the outcome as data — but until it does, guessing *explicitly*, in one
# place, pinned by a test against the real strings, beats the alternative, which
# is what this replaced: "IMAP not configured" reaching a fact as "no e-mails
# exist".
OUTCOME_OK = "ok"
OUTCOME_EMPTY = "empty"
OUTCOME_FAILED = "failed"

# A tool that did not run, or could not read what it reached. Ordered by how
# specific the marker is; matched against the lowercased text.
_FAILURE_MARKERS = (
    "no active user",
    "not configured",              # IMAP / calendar credentials missing
    "imap error", "calendar error",
    "error during search", "error during analytical search",
    "error loading document", "error accessing document",
    "not found or no access",
    "no extracted text found",         # the page was never OCR'd, not "the page is blank"
    "invalid table_index",
    "index.json could not be loaded",
    "web search failed", "web fetch failed", "search unavailable",
    "vault search failed", "vault retrieval failed",
    "no notes indexed in the vault",   # an empty index, not an empty answer
    "could not read",                  # a bot wall or a paywall stub
    "no text content extractable",
    "suchtext zu kurz",                # the call was refused before it ran
    "no search criterion given", "no search term and no time range given",
    "no url given", "query or pbrain_id required",
    "failed:",                         # `Tool 'x' failed: …` from `_call`
    "was not run",                     # arguments the model did not send as JSON
)

# It ran, and there was nothing there.
_EMPTY_MARKERS = (
    "no results found", "keine ergebnisse", "nothing found",
    "no documents match the criteria",
    "no emails found", "no calendar entries found",
    "no relevant facts found", "no relevant notes found",
    "no actions/deadlines found",
    "has no extracted tables",
    "no folders found",
)

# Only a search has a result count. A fetched page does not — and it used to get
# one anyway, because the bullet-counting fallback happily counted the list items
# in the page's own markdown: one `web_fetch_page` was recorded as 95 hits. D3
# accepts a fact backed by query+hits, so that number was not cosmetic. It made
# an unfounded claim checkable.
SEARCH_TOOLS = frozenset({
    "search", "search_exact", "vault_search", "search_memory",
    "search_emails", "search_calendar", "web_search", "get_actions",
})

# The count a tool states about itself, which beats anything counted from the
# formatting. The pagination total wins over the page size: "50 hits, showing
# 1–50 of 143" is a finding about 143 e-mails, not 50.
_TOTAL_STATEMENTS = (
    re.compile(r"showing\s*\d+\s*[–—-]\s*\d+\s*of\s*(\d+)", re.I),
)
_HIT_STATEMENTS = (
    re.compile(r"Found:\s*(\d+)\s*document", re.I),
    re.compile(r"Vault:\s*(\d+)\s*hit", re.I),
    re.compile(r"Memory:\s*(\d+)\s*relevant fact", re.I),
    re.compile(r"(\d+)\s*(?:Treffer|hits?|results?)\b", re.I),
)
# Otherwise count the entries in the list.
_HIT_PATTERNS = (
    re.compile(r"^\s*\d+\.\s+\[", re.M),        # web_search result list
    re.compile(r"^\s*\d+\.\s+#\d+", re.M),      # paperless search result list
    re.compile(r"^\s*(?:Document|Dokument)\s+#\d+", re.M),
    re.compile(r"^\s*\[E\d+\]", re.M),           # e-mail result list
    re.compile(r"^\s*[-*•]\s+", re.M),
)

# The hits below answer a *wider* query than the one that was asked, so the count
# does not belong to `source.query` and must not be presented as if it did.
_FALLBACK_MARKER = "the hits below come from a broader search"


def classify(text: str) -> str:
    """What this call did: `ok`, `empty` or `failed`."""
    stripped = (text or "").strip().lower()
    if not stripped:
        return OUTCOME_FAILED
    if any(marker in stripped for marker in _FAILURE_MARKERS):
        return OUTCOME_FAILED
    if any(marker in stripped for marker in _EMPTY_MARKERS):
        return OUTCOME_EMPTY
    return OUTCOME_OK


def _looks_empty(text: str) -> bool:
    """Nothing usable came back — whether it ran or not.

    Kept for the callers that only care that there is nothing to work with;
    everything that reasons about *absence* must use `classify()` instead.
    """
    return classify(text) in (OUTCOME_EMPTY, OUTCOME_FAILED)


def _hits_from(tool: str, text: str) -> int | None:
    """Result count for a retrieval call, or None when it is not countable.

    `Source.hits` is stripped from whatever the model wrote (it is not the
    model's to claim), and nothing was filling it back in — so the one honest
    way to state a negative finding, "I ran this query and it returned nothing",
    could never satisfy D3 and was rejected every time.

    None for anything that is not a search, and for a failed call: a count that
    is not real is worse than no count, because D3 treats query+hits as evidence.
    """
    if tool not in SEARCH_TOOLS:
        return None
    body = text or ""
    outcome = classify(body)
    if outcome == OUTCOME_FAILED:
        return None
    if outcome == OUTCOME_EMPTY:
        return 0
    if _FALLBACK_MARKER in body.lower():
        return None
    for pattern in _TOTAL_STATEMENTS:
        if match := pattern.search(body):
            return int(match.group(1))
    for pattern in _HIT_STATEMENTS:
        if match := pattern.search(body):
            return int(match.group(1))
    for pattern in _HIT_PATTERNS:
        found = len(pattern.findall(body))
        if found:
            return found
    return None


@dataclass
class ToolBelt:
    """The tools one subtask may use, plus the log of what they returned."""

    registry: Registry
    allowed_tools: list[str]
    run_id: str = ""
    user_id: str = ""
    token: str = ""
    subtask_id: str = ""
    sources_restrict: list[str] | None = None
    persist: bool = True
    records: list[ToolCallRecord] = field(default_factory=list)

    # A subtask that fires 46 web searches is not being thorough — it is looping
    # on a query that returns nothing, and it takes the search host down with
    # it. The budget is per tool so one exhausted source does not end the
    # subtask; the model is told the remaining count when it runs out.
    max_calls_per_tool: int = 12
    max_calls_total: int = 40
    # What this subtask already asked in earlier revisions — for *dedupe*, not
    # for the count. A revision re-runs the research from scratch, so charging
    # it for the previous attempt's calls left the last attempt (the one whose
    # result counts) with a budget of zero: it retrieved nothing and was
    # reported as unanswered although the earlier attempts had fetched twelve
    # pages. Repeats are still refused, which is what protects the search host.
    prior_queries: set[str] = field(default_factory=set)

    # ── what the model is offered ────────────────────────────────────────────
    def definitions(self) -> list[dict]:
        """TOOL_DEFINITIONS filtered to this agent — and to what is reachable.

        A tool the subtask may not use is not described to the model. Offering
        it and refusing afterwards wastes a turn and teaches the model that
        refusals are negotiable.
        """
        from services.chat_service import TOOL_DEFINITIONS

        usable = {t for t in self.allowed_tools if self._reachable(t)}
        return [d for d in TOOL_DEFINITIONS if d["name"] in usable]

    def _reachable(self, tool: str) -> bool:
        if tool in self.registry.forbidden_tools:
            return False
        if not self.sources_restrict:
            return True
        source = TOOL_SOURCE_TYPE.get(tool, "model")
        return source in set(self.sources_restrict) | {"computed"}

    # ── execution ────────────────────────────────────────────────────────────
    def calls_of(self, name: str) -> int:
        return sum(1 for r in self.records if r.tool == name)

    @staticmethod
    def query_key(name: str, args: dict) -> str:
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

    def _seen_query(self, name: str, args: dict) -> bool:
        """The same search twice returns the same nothing, and costs the host a
        request it may answer with a CAPTCHA next time. Spans revisions: the
        second attempt repeating the first one's twelve searches is exactly how
        one subtask made 36 requests at a single host."""
        key = self.query_key(name, args)
        return key in self.prior_queries or any(
            self.query_key(r.tool, r.args) == key for r in self.records
        )

    def exhausted_tools(self) -> list[str]:
        """Tools whose budget is used up — reported to the critic as a limit the
        subtask ran into, so 'found nothing' can be told from 'stopped looking'."""
        return sorted({r.tool for r in self.records if self.calls_of(r.tool) >= self.max_calls_per_tool})

    def dead_tools(self) -> list[str]:
        """Tools that answered nothing every single time.

        The case this exists for: a rate-limited search host answers HTTP 200
        with zero results, so 'the web has nothing on this' and 'the search was
        throttled' look identical to the model — and the run then reports the
        absence as a finding. That is a false negative presented as evidence,
        which is the worst thing this system can produce.
        """
        by_tool: dict[str, list[str]] = {}
        for record in self.records:
            by_tool.setdefault(record.tool, []).append(record.outcome)
        return sorted(
            tool for tool, outcomes in by_tool.items()
            if len(outcomes) >= 3 and all(o != OUTCOME_OK for o in outcomes)
        )

    def failed_tools(self) -> list[str]:
        """Tools that never ran successfully — one attempt is enough.

        "IMAP not configured" and "no e-mails found" are the same sentence to a
        model, and only one of them is a finding. A tool whose every call failed
        establishes no absence whatsoever, so unlike `dead_tools` this needs no
        three strikes: the single call that could not run is the whole story.
        """
        by_tool: dict[str, list[str]] = {}
        for record in self.records:
            by_tool.setdefault(record.tool, []).append(record.outcome)
        return sorted(
            tool for tool, outcomes in by_tool.items()
            if all(o == OUTCOME_FAILED for o in outcomes)
        )

    async def execute(self, name: str, args: dict) -> str:
        """Run one tool call, record it, return the text for the model."""
        if name not in self.allowed_tools:
            return f"Tool '{name}' is not available for this subtask."
        args, shaping_note = _shape_args(name, args)
        if not self._reachable(name):
            return (
                f"Tool '{name}' is blocked for this subtask "
                f"(restricted to: {', '.join(self.sources_restrict or [])})."
            )
        if len(self.records) >= self.max_calls_total:
            return (
                "Tool budget for this subtask is used up. Report what you have, "
                "and record what is still missing as a gap."
            )
        if self._seen_query(name, args):
            return (
                f"You already ran this exact {name} call in this subtask and it is "
                "not going to answer differently. Change the query, use a "
                "different source, or record what is missing as a gap."
            )
        if self.calls_of(name) >= self.max_calls_per_tool:
            return (
                f"'{name}' has been called {self.max_calls_per_tool} times for this "
                "subtask, which is the limit. Use a different source, or record "
                "what is missing as a gap — do not repeat this search."
            )

        text = await self._call(name, args)
        self._record(name, args, text)
        if shaping_note:
            text = f"{text}\n\n{shaping_note}"

        # The vault companion: fired by the wrapper, not chosen by the model,
        # so it happens on every document search rather than when the model
        # remembers to.
        if name in VAULT_COMPANION_FOR and self._reachable("vault_search"):
            if "vault_search" in self.allowed_tools:
                query = str(args.get("query") or args.get("semantic_query") or "")
                if query:
                    companion = await self._call("vault_search", {"query": query})
                    self._record("vault_search", {"query": query}, companion)
                    text = (
                        f"{text}\n\n--- Notes of the user on the same query "
                        f"(trust: user_asserted, not a document) ---\n{companion}"
                    )
        return text

    async def _call(self, name: str, args: dict) -> str:
        from services.chat_service import (
            _current_owner,
            _current_token,
            _web_fetch_mode,
            execute_tool,
        )

        owner = _current_owner.set(self.user_id)
        cred = _current_token.set(self.token)
        # Browser-first fetching for a research run. In chat the extra seconds
        # per page are felt by someone waiting; here a run already waits minutes
        # on the model, and the pages that matter — journals, registries,
        # anything behind a cookie banner — only open in a real browser.
        mode = _web_fetch_mode.set("crawl4ai")
        try:
            text, _docs, _extras = await execute_tool(name, args)
            return text or ""
        except Exception as exc:                     # a failing tool is a gap, not a crash
            _log.warning("werkbank v2 tool %s failed: %s", name, exc)
            return f"Tool '{name}' failed: {exc}"
        finally:
            _current_owner.reset(owner)
            _current_token.reset(cred)
            _web_fetch_mode.reset(mode)

    def _record(self, name: str, args: dict, text: str) -> None:
        outcome = classify(text)
        record = ToolCallRecord(
            source_id=f"s{len(self.records) + 1}",
            tool=name,
            args=dict(args),
            raw_text=text,
            trust=self.registry.trust_for(name),
            source_type=TOOL_SOURCE_TYPE.get(name, "model"),
            ref=_ref_for(name, args),
            hits=_hits_from(name, text),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            # Nothing in an error message is evidence. Leaving a failed call
            # quotable let a fact hang off "IMAP not configured" and pass D2,
            # because the quote really was in the text the tool returned.
            quotable=name not in SNIPPET_ONLY_TOOLS and outcome != OUTCOME_FAILED,
            outcome=outcome,
        )
        self.records.append(record)
        if self.persist and self.run_id:
            from werkbank.v2 import store

            store.log_tool_call(
                self.run_id, self.user_id, self.subtask_id,
                source_id=record.source_id, tool=record.tool, args=record.args,
                raw_text=record.raw_text, trust=record.trust, ref=record.ref,
                hits=record.hits,
            )

    # ── what the checks read ─────────────────────────────────────────────────
    def raw_texts(self) -> dict[str, str]:
        """source id → retrieved text, for D2. Snippet-only tools are excluded:
        text that must not be quoted must not be quotable."""
        return {r.source_id: r.raw_text for r in self.records if r.quotable}

    def trust_by_source(self) -> dict[str, str]:
        # A call that did not run carries the trust of no tool. Leaving it at
        # `authoritative` because the tool *would* have been authoritative is how
        # "Dokument #163 not found or no access" ends up as the strongest source
        # in a report.
        return {
            r.source_id: ("model" if r.outcome == OUTCOME_FAILED else r.trust)
            for r in self.records
        }

    def type_by_source(self) -> dict[str, str]:
        return {r.source_id: r.source_type for r in self.records}

    @property
    def call_count(self) -> int:
        return len(self.records)

    def catalogue(self) -> str:
        """The sources, as the model is told to cite them."""
        if not self.records:
            return "(no sources retrieved yet)"
        return "\n".join(
            f"- {r.source_id}: {r.tool}({_short(r.args)}) → {_outcome_note(r)}"
            for r in self.records
        )


# Full message bodies per call. A survey of a mailbox is a job for headers: one
# search with `detail=full` over 50 messages took four and a half minutes, and a
# subtask that made three of them spent its whole wall clock inside IMAP for text
# it then had to summarise anyway.
MAX_FULL_EMAILS = 15


def _shape_args(name: str, args: dict) -> tuple[dict, str]:
    """Clamp a call that would cost minutes for text nobody reads.

    Returns the arguments to use and a note for the model, so the limit is
    something it can work with rather than a silent difference between what it
    asked for and what it got.
    """
    if name != "search_emails":
        return args, ""
    detail = str(args.get("detail") or "snippet")
    try:
        wanted = int(args.get("max_results") or 10)
    except (TypeError, ValueError):
        return args, ""
    if detail != "full" or wanted <= MAX_FULL_EMAILS:
        return args, ""
    return (
        {**args, "max_results": MAX_FULL_EMAILS},
        f"NOTE: full message bodies are limited to {MAX_FULL_EMAILS} per call "
        f"(you asked for {wanted}). Survey with detail='headers' — dates, senders "
        "and subjects for hundreds of messages in one call — then read the few "
        "threads that matter with detail='full' and a narrow query.",
    )


def _outcome_note(record: ToolCallRecord) -> str:
    """What the model is told about a call, including when it did not run.

    A failed call reaching the model as an ordinary source is how "the search
    host refused me" becomes "there is nothing on the web about this".
    """
    if record.outcome == OUTCOME_FAILED:
        return "FAILED — this call did not run; it is not evidence of absence"
    if record.outcome == OUTCOME_EMPTY:
        return "ran, returned nothing (this IS evidence of absence)"
    if not record.quotable:
        return "NOT quotable (search result list)"
    return "quotable"


# Arguments that identify the source. Truncating one is not cosmetic: the model
# copies the catalogue entry into `sources[].ref`, so a shortened URL became the
# link in the report and every source was unreachable.
_IDENTIFYING_ARGS = {"url", "document_id", "page"}


def _short(args: dict) -> str:
    return ", ".join(
        f"{k}={v}" if k in _IDENTIFYING_ARGS else f"{k}={str(v)[:40]}"
        for k, v in args.items()
    )


def _ref_for(name: str, args: dict) -> str:
    """A human-followable pointer, so a fact marker in the report can link out."""
    if doc_id := args.get("document_id"):
        page = args.get("page")
        return f"doc:{doc_id}#p{page}" if page else f"doc:{doc_id}"
    if url := args.get("url"):
        return str(url)
    if query := args.get("query"):
        return f"query:{str(query)[:60]}"
    return ""


def apply_tool_trust(result, belt: ToolBelt):
    """Overwrite what the model claimed about its sources with the tool log.

    The model is not asked to be honest about provenance; it is not consulted.
    Trust, source type and `retrieved_at` come from the record of the call that
    produced the text.
    """
    trust = belt.trust_by_source()
    types = belt.type_by_source()
    stamps = {r.source_id: r.retrieved_at for r in belt.records}
    refs = {r.source_id: r.ref for r in belt.records}
    hits = {r.source_id: r.hits for r in belt.records}
    queries = {
        r.source_id: str(r.args.get("query") or r.args.get("semantic_query") or "")
        for r in belt.records
    }

    from werkbank.v2.models import SourceTrust

    for fact in result.facts:
        for source in fact.sources:
            if source.id in trust:
                # Coerced explicitly: pydantic does not validate plain attribute
                # assignment, so a raw string here would leave a field that
                # looks like an enum and is not one.
                source.trust = SourceTrust(trust[source.id])
                source.type = types.get(source.id, source.type)
                source.retrieved_at = stamps.get(source.id, source.retrieved_at)
                # The log wins over the model here too: a ref it retyped can be
                # truncated or wrong, and a wrong link is worse than none.
                source.ref = refs.get(source.id) or source.ref
                # Both come from the call, not from the model: the query it ran
                # and how many results came back. Without them a negative
                # finding ("this query returned nothing") has no way to be
                # checked, and D3 throws it out.
                source.hits = hits.get(source.id, source.hits)
                source.query = source.query or queries.get(source.id, "")
            else:
                # A source id that no tool call produced: the model invented the
                # provenance. It keeps the lowest trust and D2 will reject any
                # quote hanging off it.
                source.trust = SourceTrust.MODEL
    return result

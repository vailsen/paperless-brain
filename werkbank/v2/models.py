"""Werkbank v2 — the data model the whole pipeline is built on.

The design principle, from `docs/werkbank-architecture.md`:

    Honesty does not come from prompt instructions. It comes from the schema
    having a place for "I don't know", and from deterministic code checking
    what the model claims.

Three consequences visible here:

- **`Fact` is the citable substrate.** Prose (`narrative`) may only reference
  fact ids, never carry claims of its own, so the writer cannot write prose
  from prose from prose.
- **`Gap` is a first-class field.** Without a paved path for "not found", a
  model takes the path to "yes".
- **Some fields are never filled by the LLM**: `Source.trust`, `Fact.confidence`,
  `Source.hits` and the whole `SelfCheck`. They are set by the calling code from
  the tool log. `strip_llm_controlled()` enforces that at the parse boundary —
  a model that writes them anyway is overruled rather than trusted.
"""

from __future__ import annotations

import copy
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

# ── Enums ─────────────────────────────────────────────────────────────────────


class Evidence(str, Enum):
    """What backs a claim. Deliberately not a number.

    An LLM's numeric confidence is not calibrated — in practice everything
    lands between 0.85 and 0.95 and none of it is checkable. `evidence` is tied
    to something that can be verified by code.
    """

    QUOTE = "quote"                      # verbatim from retrieved text (D2)
    COMPUTED = "computed"                # arithmetic or a metadata query (D3)
    DERIVED = "derived"                  # from other facts (D4)
    MODEL_KNOWLEDGE = "model_knowledge"  # parametric memory — always suspect
    NONE = "none"


class SourceTrust(str, Enum):
    """Set by the tool wrapper, never by the model, never overridable by an agent."""

    AUTHORITATIVE = "authoritative"   # a real document, produced independently of the user
    USER_ASSERTED = "user_asserted"   # the user or their circle said so, unverified
    EXTERNAL = "external"             # third parties; recency and bias unknown
    COMPUTED = "computed"             # metadata query or arithmetic
    DERIVED = "derived"               # extraction/summary of a source, not its wording
    MODEL = "model"                   # parametric knowledge, no evidence


class FactKind(str, Enum):
    STATEMENT = "statement"
    TABLE = "table"
    LIST = "list"
    EXCERPT = "excerpt"
    FIGURE = "figure"


class SubtaskStatus(str, Enum):
    TODO = "todo"
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    UNRESOLVABLE = "unresolvable"


class GapReason(str, Enum):
    NOT_FOUND = "not_found"
    SOURCE_UNAVAILABLE = "source_unavailable"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"


class CriticDecision(str, Enum):
    ACCEPT = "accept"
    REVISE = "revise"
    UNRESOLVABLE = "unresolvable"


class CriterionVerdict(str, Enum):
    MET = "met"
    PARTIAL = "partial"
    UNMET = "unmet"


class CoverageVerdict(str, Enum):
    COVERED = "covered"
    PARTIAL = "partial"
    UNCOVERED = "uncovered"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DepthBudget(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class BudgetConfig(BaseModel):
    max_subtasks: int
    max_revisions: int
    run_plan_critic: bool


# An enum rather than free text: the model has no feel for wall-clock time or
# token cost, so the budget is the user's decision, not the planner's.
DEPTH_BUDGETS: dict[DepthBudget, BudgetConfig] = {
    DepthBudget.QUICK: BudgetConfig(max_subtasks=3, max_revisions=0, run_plan_critic=False),
    DepthBudget.STANDARD: BudgetConfig(max_subtasks=8, max_revisions=2, run_plan_critic=True),
    DepthBudget.DEEP: BudgetConfig(max_subtasks=20, max_revisions=3, run_plan_critic=True),
}

FACT_ID_RE = re.compile(r"^st\d+\.f\d+$")
SUBTASK_ID_RE = re.compile(r"^st\d+$")
# Markers the narrative and the report may carry: [st3.f1]
MARKER_RE = re.compile(r"\[(st\d+\.f\d+)\]")


# ── Shape coercion ────────────────────────────────────────────────────────────


_LIST_ITEM_SEP = re.compile(r'"\s*,\s*"')
# Junk a model leaves around a serialised list: a trailing comma from an
# interrupted generation, a stray semicolon, quotes around the whole thing.
_BLOB_EDGES = " \t\r\n,;"


# One quote is escaped per round trip through the parser; a payload needing more
# than this is not a punctuation problem any more.
_MAX_QUOTE_REPAIRS = 400
# A quote that *opens* a string comes right after a structural character. Escaping
# one of those turns a broken document into rubble, so the repair stops there.
_OPENS_STRING = re.compile(r"[\{\[,:]\s*$")


def _escape_quote_before(text: str, pos: int) -> str | None:
    """Escape the quote that closed a string just before the parser gave up.

    Returns None when the quote found there opens a string — the document is
    then broken in some other way and escaping would only deepen the damage.
    """
    index = text.rfind('"', 0, max(pos, 0))
    while index > 0:
        preceding = len(text[:index]) - len(text[:index].rstrip("\\"))
        if preceding % 2 == 0:
            break                                   # not an already-escaped quote
        index = text.rfind('"', 0, index)
    if index <= 0 or _OPENS_STRING.search(text[:index]):
        return None
    return text[:index] + '\\"' + text[index + 1:]


def repair_json(text: str) -> str:
    """Escape content quotes a model left unescaped inside JSON strings.

    German prose puts a closing `"` inside the value — `„Prion" und ist unter …`
    — and that quote ends the JSON string three words early. Everything after it
    is garbage to any parser, so a complete, correct answer is thrown away over a
    typographic convention.

    A lookahead rule ("a real closing quote is followed by `,` `:` `}` `]`")
    looks like it settles it and does not: `(als „M.Milbich", Bauleiter)` is
    indistinguishable from the end of a value, and that one shape cost a subtask
    every fact it had found after twelve tool calls. So this asks the parser
    instead of guessing — wherever `json` stops, the quote immediately before
    that point is the one that closed a string too early. Escape it, parse again,
    repeat. Text that cannot be repaired comes back unchanged and the caller
    still rejects it: this widens what is *parsed*, never what is *believed*.
    """
    import json

    repaired = text
    for _ in range(_MAX_QUOTE_REPAIRS):
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError as exc:
            nxt = _escape_quote_before(repaired, exc.pos)
            if nxt is None:
                return repaired
            repaired = nxt
        except ValueError:
            return repaired
    return repaired


def _parse_bracketed(text: str) -> list | None:
    """A JSON-looking array, parsed by whatever manages it.

    Strict parsing fails more often than one would like. Real examples, all from
    the same model on the same field:

    - German typographic quotes inside: `["„ETCS-Antennen" bedeutet …", "…"]`
      closes its first string early and is not valid JSON.
    - A trailing comma: `[…],` — `json` refuses it, and `ast` reads it as a
      *tuple containing the list*, which is not the list.

    Returns None when nothing works, which is the important case: a bracketed
    blob must never be accepted as a single item. That is worse than an error —
    the user would confirm one giant "assumption" that is really five.
    """
    import ast
    import json

    for parser in (json.loads, ast.literal_eval, lambda t: json.loads(repair_json(t))):
        try:
            parsed = parser(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        parsed = _unwrap(parsed)
        if isinstance(parsed, list):
            return parsed

    inner = text[1:-1].strip()
    if not (inner.startswith('"') and inner.endswith('"')):
        return None
    items = [part.strip().strip('"').strip() for part in _LIST_ITEM_SEP.split(inner[1:-1])]
    items = [item for item in items if item]
    return items or None


def _unwrap(value):
    """`([…],)` and `[[…]]` both mean the list inside."""
    while isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(
        value[0], (list, tuple)
    ):
        value = value[0]
    return list(value) if isinstance(value, (list, tuple)) else value


def _as_list(value):
    """A list field the model sent as something else, turned back into a list.

    Models serialise a list into a string often enough that it is worth
    handling: seen in the wild as `"assumptions": "[\"a\", \"b\"]"` while every
    other list field in the same answer came back correctly. Retrying does not
    help — the model reproduces its own habit, so three calls fail on the same
    line and the user sees a schema error for an answer that was otherwise fine.

    Only unambiguous shapes are converted. Anything else is passed through
    untouched so validation still rejects it and the retry sees the defect: this
    widens what is *accepted*, never what is *believed*.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        # One item sent unwrapped. Seen as `"facts": {…}` for a single fact,
        # which failed as "Input should be a valid list" three attempts running
        # and cost the subtask everything it had retrieved.
        return [value]
    if isinstance(value, list):
        # A list holding one serialised list — the same blob, one layer deeper.
        if len(value) == 1 and isinstance(value[0], str):
            inner = _as_list(value[0])
            if isinstance(inner, list) and len(inner) > 1:
                return inner
        return value
    if not isinstance(value, str):
        return value

    text = value.strip(_BLOB_EDGES)
    if not text:
        return []
    if text[0] in "[(" and text[-1] in "])":
        # A bracketed blob is a list or it is an error — never one long item.
        return _parse_bracketed(text) or value
    if "\n" in text:                    # a bullet list rendered as one string
        items = [line.lstrip("-*• \t").strip() for line in text.splitlines()]
        return [item for item in items if item]
    return [text]                       # a single item, sent unwrapped


class _Coercing(BaseModel):
    """Base for every model an LLM fills in. Fixes list-shaped strings first."""

    @model_validator(mode="before")
    @classmethod
    def _coerce_list_fields(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            annotation = str(field.annotation)
            if "list" in annotation and name in out:
                out[name] = _as_list(out[name])
        return out


# ── Leaf models ───────────────────────────────────────────────────────────────


class Source(_Coercing):
    """Where a fact came from.

    `quote` is the single deterministic anti-hallucination device in the whole
    system: it is matched against the text the tool actually returned (D2).
    For metadata queries there is no quote to give, which is why `query` and
    `hits` exist — those are checked by D3 instead.
    """

    id: str
    type: str                                  # paperless | vault | web | email | calendar | note
    trust: SourceTrust = SourceTrust.MODEL     # overwritten from the tool log
    ref: str = ""                              # doc:1423#p2, a URL, a vault path
    retrieved_at: str = ""
    quote: str = ""
    query: str = ""
    hits: int | None = None                    # set by code, never by the model


class Gap(_Coercing):
    """An unanswered part of the question, named rather than papered over."""

    question: str
    reason: GapReason
    suggested_source: str = ""
    # Set by code, not by the model: why the reason is what it is. A gap that
    # changed from "not found" to "source unavailable" has to say so, or the
    # report reads like an absence was established.
    note: str = ""


class Fact(_Coercing):
    """The smallest unit that can be accepted or rejected *as a whole*.

    Not "one line": a table from one source is one fact. A table merged from
    five sources is a `derived` fact carrying all five in `derived_from`, and
    it is produced by the synthesizer rather than by a researcher.
    """

    id: str
    kind: FactKind = FactKind.STATEMENT
    claim: str
    evidence: Evidence
    expression: str = ""                       # arithmetic, for evidence=computed
    sources: list[Source] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    confidence: Confidence | None = None       # derived by code from evidence+trust

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not FACT_ID_RE.match(v):
            raise ValueError(f"fact id must look like st3.f1, got {v!r}")
        return v

    @model_validator(mode="after")
    def _evidence_needs_its_backing(self) -> Fact:
        if self.evidence is Evidence.QUOTE and not any(s.quote.strip() for s in self.sources):
            raise ValueError(f"{self.id}: evidence=quote needs a source carrying a quote")
        if self.evidence is Evidence.DERIVED and not self.derived_from:
            raise ValueError(f"{self.id}: evidence=derived needs derived_from")
        return self

    @property
    def subtask_id(self) -> str:
        return self.id.split(".", 1)[0]


class SelfCheck(BaseModel):
    """Filled by code from the tool log, never by the model.

    A runner whose facts are all `model_knowledge` although tools were
    available is rejected deterministically (D5) — that is the "answered
    without looking anything up" failure, caught structurally instead of
    argued about in a prompt.
    """

    claims_without_source: int = 0
    sources_fetched: int = 0
    tool_calls: int = 0


# ── Plan ──────────────────────────────────────────────────────────────────────


class Brief(_Coercing):
    """The briefer's output, confirmed by the user before anything runs.

    `acceptance_criteria` is the most important lever in the system: without
    criteria registered *before* the research, the critic invents the standard
    afterwards and always passes. That is the default behaviour of LLM
    self-evaluation, not an edge case.
    """

    original_request: str                      # verbatim, carried through untouched
    goal: str
    out_of_scope: list[str] = Field(default_factory=list)
    deliverable_format: str = "Bericht"
    assumptions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    depth_budget: DepthBudget = DepthBudget.STANDARD

    @property
    def budget(self) -> BudgetConfig:
        return DEPTH_BUDGETS[self.depth_budget]


class Subtask(_Coercing):
    """One planned unit of work, assigned to exactly one agent."""

    subtask_id: str
    question: str
    agent: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    covers_criteria: list[int] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    sources_restrict: list[str] | None = None

    @field_validator("subtask_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not SUBTASK_ID_RE.match(v):
            raise ValueError(f"subtask id must look like st3, got {v!r}")
        return v


# ── Results ───────────────────────────────────────────────────────────────────


class SubtaskResult(_Coercing):
    """What a runner returns. Prose survives, but derived from facts."""

    subtask_id: str
    revision: int = 0
    status: SubtaskStatus = SubtaskStatus.OK
    question: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    covers_criteria: list[int] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    agent: str = ""
    sources_restrict: list[str] | None = None
    model: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    facts: list[Fact] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    narrative: str = ""
    self_check: SelfCheck = Field(default_factory=SelfCheck)

    @model_validator(mode="after")
    def _facts_belong_to_this_subtask(self) -> SubtaskResult:
        for fact in self.facts:
            if fact.subtask_id != self.subtask_id:
                raise ValueError(
                    f"fact {fact.id} does not belong to subtask {self.subtask_id}"
                )
        return self

    def fact_ids(self) -> set[str]:
        return {f.id for f in self.facts}


class CriterionCheck(_Coercing):
    """One acceptance criterion, judged with fact ids as the receipt.

    A criterion with no referenced fact id is forced to `unmet` in code — that
    is the difference between a verdict and a compliment.
    """

    criterion: str
    verdict: CriterionVerdict
    fact_ids: list[str] = Field(default_factory=list)


class CriticVerdict(_Coercing):
    decision: CriticDecision
    criteria: list[CriterionCheck] = Field(default_factory=list)
    defects: list[str] = Field(default_factory=list)   # concrete, checkable, for the revision


class CoverageCheck(_Coercing):
    """The plan critic's only job: which brief criterion is not covered."""

    criterion_index: int
    verdict: CoverageVerdict
    subtask_ids: list[str] = Field(default_factory=list)


# ── Run ───────────────────────────────────────────────────────────────────────


class RunState(BaseModel):
    """Everything the reflection generator reads. Nothing here is prose."""

    run_id: str = ""
    user_id: str = ""
    model: str = ""
    brief: Brief | None = None
    subtasks: list[Subtask] = Field(default_factory=list)
    results: dict[str, SubtaskResult] = Field(default_factory=dict)
    # Status of subtasks that have no result yet — the only way the board can
    # tell "waiting" from "working on it right now". A finished subtask carries
    # its status on its result instead.
    statuses: dict[str, SubtaskStatus] = Field(default_factory=dict)
    verdicts: dict[str, CriticVerdict] = Field(default_factory=dict)
    plan_coverage: list[CoverageCheck] = Field(default_factory=list)
    capped_subtasks: list[str] = Field(default_factory=list)
    flagged_paragraphs: list[str] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def status_of(self, subtask_id: str) -> SubtaskStatus:
        result = self.results.get(subtask_id)
        if result is not None:
            return result.status
        return self.statuses.get(subtask_id, SubtaskStatus.TODO)

    def all_facts(self) -> list[Fact]:
        return [f for r in self.results.values() for f in r.facts]

    def fact_by_id(self, fact_id: str) -> Fact | None:
        for fact in self.all_facts():
            if fact.id == fact_id:
                return fact
        return None


# ── Code-owned derivations ────────────────────────────────────────────────────

# Confidence is a function of what backs the claim and how much the source is
# worth — both of which the code knows and the model does not get asked about.
_CONFIDENCE_TABLE: dict[tuple[Evidence, SourceTrust], Confidence] = {}


def derive_confidence(evidence: Evidence, trust: SourceTrust | None) -> Confidence:
    """Confidence from evidence + source trust. Never asked of the model."""
    if evidence is Evidence.MODEL_KNOWLEDGE or evidence is Evidence.NONE:
        return Confidence.LOW
    if evidence is Evidence.QUOTE:
        if trust is SourceTrust.AUTHORITATIVE:
            return Confidence.HIGH
        if trust in (SourceTrust.USER_ASSERTED, SourceTrust.EXTERNAL, SourceTrust.DERIVED):
            return Confidence.MEDIUM
        return Confidence.MEDIUM
    if evidence is Evidence.COMPUTED:
        return Confidence.HIGH if trust is SourceTrust.COMPUTED else Confidence.MEDIUM
    if evidence is Evidence.DERIVED:
        return Confidence.MEDIUM
    return Confidence.LOW


def apply_derived_confidence(fact: Fact) -> Fact:
    """Set `confidence` from the strongest source. Mutates and returns the fact."""
    best = None
    order = [
        SourceTrust.AUTHORITATIVE,
        SourceTrust.COMPUTED,
        SourceTrust.DERIVED,
        SourceTrust.EXTERNAL,
        SourceTrust.USER_ASSERTED,
        SourceTrust.MODEL,
    ]
    for trust in order:
        if any(s.trust is trust for s in fact.sources):
            best = trust
            break
    fact.confidence = derive_confidence(fact.evidence, best)
    return fact


def relabel_mislabelled_evidence(payload: dict) -> dict:
    """`derived` without `derived_from` is a label mistake, not a missing fact.

    Researchers reach for "derived" when they mean "I read this and put it in my
    own words", and the schema then rejects the entire answer. One observed
    subtask fetched six case-report pages, mislabelled its facts, failed
    validation three times, and was reported as *nothing found* — with 8 KB of
    relevant text sitting in the tool log.

    So the label is corrected to what the evidence actually is. Nothing is
    believed on the strength of it: a relabelled `quote` still has to survive D2
    against the retrieved text, and a `computed` still needs query and hits.
    Only the word changes, never the checking.

    Applied to model payloads only. A `Fact` built in code with `derived` and no
    parent is a bug and still raises.
    """
    for fact in payload.get("facts") or []:
        if not isinstance(fact, dict) or fact.get("evidence") != "derived":
            continue
        if fact.get("derived_from"):
            continue
        sources = [s for s in (fact.get("sources") or []) if isinstance(s, dict)]
        if any((s.get("quote") or "").strip() for s in sources):
            fact["evidence"] = "quote"
        elif any((s.get("query") or "").strip() or s.get("type", "").startswith("search")
                 for s in sources):
            # `hits` is filled in from the tool log *after* this runs, so it
            # cannot be part of the test here — the query is what identifies a
            # metadata claim at this point.
            fact["evidence"] = "computed"
        else:
            # Nothing backs it. `model_knowledge` is the honest label and stays
            # visible as such in the report rather than passing as sourced.
            fact["evidence"] = "model_knowledge"
    return payload


def strip_llm_controlled(payload: dict) -> dict:
    """Remove the fields the model is not allowed to decide, before parsing.

    Cheaper than arguing with the prompt: whatever it wrote for `trust`,
    `hits`, `confidence` or `self_check` is dropped here and filled from the
    tool log afterwards.
    """
    # Deep copy: the caller keeps the raw model response for the prompt log,
    # and Phase 4 verifies behaviour against exactly that record. Editing it
    # from under them would falsify the evidence.
    payload = copy.deepcopy(payload)
    payload.pop("self_check", None)
    for fact in payload.get("facts") or []:
        if isinstance(fact, dict):
            fact.pop("confidence", None)
            for source in fact.get("sources") or []:
                if isinstance(source, dict):
                    source.pop("trust", None)
                    source.pop("hits", None)
    return payload

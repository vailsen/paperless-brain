"""werkbank/roles/splitter.py — decomposes a refined goal into a sub-task DAG.

Full implementation: generation constraint (Ollama format= / Claude tool-use),
8-step deterministic validation, retry-with-feedback (cap 2),
degradation fallback, and temp-ref → DB-ID remapping.
"""

from __future__ import annotations

from werkbank import repository
from werkbank.models import SubTaskSpec

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_SUBTASKS = 12
_RETRY_CAP = 2
_REQUIRED_FIELDS = {"ref", "instruction", "archetype", "success_criteria", "depends_on"}
_TOOL_NAME = "create_subtasks"

# JSON Schema enforced via Ollama format= or Claude tool-use input_schema.
SUBTASK_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "instruction": {"type": "string"},
                    "archetype": {"type": "string"},
                    "success_criteria": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "ref",
                    "instruction",
                    "archetype",
                    "success_criteria",
                    "depends_on",
                ],
            },
        }
    },
    "required": ["subtasks"],
}

# Moved to Settings in Phase 7.
DEFAULT_SYSTEM_PROMPT = """\
You are a task decomposer for autonomous research systems.

Your job: decompose the work order into concrete, independently executable
sub-tasks and assign a suitable archetype to each.

Rules for good sub-tasks:
- Each sub-task is a complete, self-explanatory instruction.
- Choose the archetype only from the given list.
- depends_on contains only refs of earlier entries — no cycles, no self-reference.
- At most {max_subtasks} sub-tasks. Less is more.
- success_criteria: short, verifiable criterion (1 sentence).
- Write instructions and success criteria in the same language as the work order.

Archetype selection (by data source):
- Searching the document archive / stored documents → retriever. This includes
  product data, data sheets, contracts, invoices and correspondence — such
  content typically lives in the archive. When in doubt, retriever before researcher.
- Web research ONLY for information that is certainly not in the archive
  (current market prices, news, third-party providers, dates on the internet) → researcher.
- Searching the user's emails or calendar → secretary.
- PROCESSING, SUMMARIZING or FORMATTING already collected data
  (e.g. "Summarize T1 and T2", "Create a report from the results") → writer.
  The writer receives the results of its depends_on tasks as context — it needs
  no research of its own.

Example:
Order: "Check whether my car insurance is too expensive and find cheaper alternatives."
Good plan:
1. ref=s1, archetype=retriever, depends_on=[]:
   instruction: "Find the current car insurance policy in the archive: provider,
   tariff, annual premium, coverage. Cite document IDs."
   success_criteria: "Provider and annual premium of the current policy named with document ID."
2. ref=s2, archetype=researcher, depends_on=[]:
   instruction: "Research current comparison offers for car insurance with
   comparable coverage on the web."
   success_criteria: "At least 3 comparison offers with price and source (URL)."
3. ref=s3, archetype=writer, depends_on=[s1, s2]:
   instruction: "Compare the current policy with the researched offers
   and produce a tabular recommendation."
   success_criteria: "Tabular comparison with a clear recommendation."\
""".format(max_subtasks=MAX_SUBTASKS)


# ── Exception ─────────────────────────────────────────────────────────────────


class SplitterParseError(Exception):
    pass


# ── Validation ────────────────────────────────────────────────────────────────


def _topological_sort(specs: list[SubTaskSpec]) -> list[SubTaskSpec]:
    """Kahn's algorithm. Raises SplitterParseError on cycle."""
    ref_to_spec = {s.ref: s for s in specs}
    in_deg = {s.ref: len(s.depends_on) for s in specs}
    ready = [s for s in specs if not s.depends_on]

    result: list[SubTaskSpec] = []
    while ready:
        node = ready.pop(0)
        result.append(node)
        for s in specs:
            if node.ref in s.depends_on:
                in_deg[s.ref] -= 1
                if in_deg[s.ref] == 0:
                    ready.append(s)

    if len(result) != len(specs):
        cycle_refs = {s.ref for s in specs} - {s.ref for s in result}
        raise SplitterParseError(f"Cycle in the dependency graph: {cycle_refs}")

    for i, s in enumerate(result):
        s.order_index = i
    return result


def _validate_specs(
    raw_subtasks: object,
    archetype_names: set[str],
) -> list[SubTaskSpec]:
    """8-step deterministic validation. Returns topologically sorted SubTaskSpec list."""

    # Step 3 — non-empty list + length
    if not isinstance(raw_subtasks, list) or len(raw_subtasks) == 0:
        raise SplitterParseError("'subtasks' is empty or not an array.")
    if len(raw_subtasks) > MAX_SUBTASKS:
        raise SplitterParseError(
            f"Too many sub-tasks: {len(raw_subtasks)} > {MAX_SUBTASKS}."
        )

    specs: list[SubTaskSpec] = []
    refs_seen: set[str] = set()

    for i, item in enumerate(raw_subtasks):
        # Step 4 — required fields + types
        if not isinstance(item, dict):
            raise SplitterParseError(f"Sub-task {i} is not an object.")
        missing = _REQUIRED_FIELDS - item.keys()
        if missing:
            raise SplitterParseError(f"Sub-task {i} is missing fields: {missing}.")
        if not isinstance(item["ref"], str) or not item["ref"].strip():
            raise SplitterParseError(
                f"Sub-task {i}: 'ref' must be a non-empty string."
            )
        if not isinstance(item["instruction"], str) or not item["instruction"].strip():
            raise SplitterParseError(f"Sub-task {i}: 'instruction' is empty.")
        if not isinstance(item["depends_on"], list):
            raise SplitterParseError(f"Sub-task {i}: 'depends_on' must be a list.")

        ref = item["ref"].strip()

        # Step 5 — ref uniqueness
        if ref in refs_seen:
            raise SplitterParseError(f"Duplicate ref: '{ref}'.")
        refs_seen.add(ref)

        # Step 6 — archetype exists → fallback to retriever with warning
        archetype = str(item.get("archetype", "")).strip()
        if archetype not in archetype_names:
            print(
                f"[splitter] Unknown archetype '{archetype}' → fallback: retriever"
            )
            archetype = "retriever"

        # Step 7a — no self-dependency
        deps = [str(d).strip() for d in item["depends_on"]]
        if ref in deps:
            raise SplitterParseError(f"Sub-task '{ref}' references itself.")

        specs.append(
            SubTaskSpec(
                ref=ref,
                instruction=item["instruction"].strip(),
                archetype=archetype,
                success_criteria=str(item.get("success_criteria", "")).strip(),
                depends_on=deps,
            )
        )

    # Step 7b — referential integrity
    all_refs = {s.ref for s in specs}
    for s in specs:
        unknown = set(s.depends_on) - all_refs
        if unknown:
            raise SplitterParseError(
                f"Sub-task '{s.ref}' references unknown refs: {unknown}."
            )

    # Step 8 — acyclicity (raises on cycle)
    return _topological_sort(specs)


# ── Self-critique pass ────────────────────────────────────────────────────────

_CRITIQUE_SYSTEM_PROMPT = """\
You are a granularity reviewer for sub-task plans.

Check each sub-task: does it contain several INDEPENDENT units of work that
could sensibly be executed separately (e.g. "Search X and Y and summarize" → 3 tasks)?

Rules:
- Split only on true independence — no artificial inflation.
- Split steps inherit the depends_on of the original task; follow-up steps
  depend on their immediate predecessor.
- Keep the archetypes from the original.
- Total ≤ {max_subtasks} sub-tasks — if that would be exceeded: no changes.
- If there is nothing to split: return the list unchanged.
- Respond only with the JSON tool call.\
""".format(max_subtasks=MAX_SUBTASKS)


def _specs_to_text(specs: list[SubTaskSpec]) -> str:
    lines = []
    for s in specs:
        deps = ", ".join(s.depends_on) if s.depends_on else "—"
        lines.append(
            f"[{s.ref}] archetype: {s.archetype}  depends_on: {deps}\n"
            f"  Task: {s.instruction}\n"
            f"  Success criterion: {s.success_criteria}"
        )
    return "\n\n".join(lines)


async def _self_critique_pass(
    specs: list[SubTaskSpec],
    refined_request: str,
    archetype_names: set[str],
    *,
    model: str,
    user_id: str,
    token: str,
) -> list[SubTaskSpec]:
    """One bounded expansion pass. Returns original specs on any failure."""
    from werkbank.llm_lane import complete_structured
    from werkbank.settings_store import (
        PROMPT_SPLITTER_CRITIQUE, TOKENS_SPLITTER_CRITIQUE,
        get_prompt, get_tokens,
    )

    system_prompt = get_prompt(PROMPT_SPLITTER_CRITIQUE, _CRITIQUE_SYSTEM_PROMPT)
    user_content = (
        f"Work order: {refined_request}\n\n"
        f"Current plan:\n{_specs_to_text(specs)}\n\n"
        "Review and return the (possibly expanded) sub-task list."
    )
    try:
        raw = await complete_structured(
            system_prompt,
            [{"role": "user", "content": user_content}],
            model=model,
            user_id=user_id,
            token=token,
            json_schema=SUBTASK_JSON_SCHEMA,
            tool_name=_TOOL_NAME,
            max_tokens=get_tokens(TOKENS_SPLITTER_CRITIQUE, default=8_000),
            temperature=0.1,
        )
        expanded = _validate_specs(raw.get("subtasks"), archetype_names)
        # Expansion-only guard: the critique pass may split tasks, never shrink
        # or rewrite the plan. Anything else keeps the original.
        if len(expanded) > len(specs):
            print(f"[splitter] self-critique expanded {len(specs)} → {len(expanded)} subtasks")
            return expanded
        print("[splitter] self-critique: no expansion — keeping original plan")
        return specs
    except Exception as exc:
        print(f"[splitter] self-critique failed, keeping original plan: {exc}")
        return specs


# ── Main entry point ──────────────────────────────────────────────────────────

# e5 cosine distance, measured on live data: on-topic hits ≈ 0.10–0.13,
# off-topic floor ≈ 0.16+. Beyond this the hit is too weak to claim the
# archive covers the topic.
_ARCHIVE_HINT_MAX_DIST = 0.15


async def _archive_hint(refined_request: str) -> str:
    """Deterministic peek into the documents index.

    The splitter LLM cannot know what the archive contains, so it tends to
    default to web research. This queries Chroma with the request and, on
    strong hits, appends a hint that forces doc-covered sub-tasks onto the
    retriever archetype. Pure Python steering — no extra LLM call.
    """
    try:
        from services.clients import chroma

        hits = await chroma.query(query_texts=[refined_request], n_results=4)
        lines: list[str] = []
        seen: set = set()
        for h in hits[0] if hits else []:
            if h.get("distance", 1.0) > _ARCHIVE_HINT_MAX_DIST:
                continue
            doc_id = (h.get("metadata") or {}).get("paperless_id")
            if doc_id in seen:
                continue
            seen.add(doc_id)
            snippet = (h.get("document") or "").replace("\n", " ")[:160]
            lines.append(f"- Document #{doc_id}: {snippet}")
        if not lines:
            return ""
        return (
            "\n\nNote (automatic archive check): the document archive contains "
            "documents relevant to this order:\n"
            + "\n".join(lines)
            + "\nSub-tasks whose information lies in these documents MUST use "
            "archetype=retriever. Use researcher only for information that "
            "is not in the archive."
        )
    except Exception as exc:
        print(f"[splitter] archive hint failed: {exc}")
        return ""


def _fallback_spec(refined_request: str) -> list[SubTaskSpec]:
    """Single researcher sub-task — used when all retries fail."""
    return [
        SubTaskSpec(
            ref="s1",
            instruction=refined_request,
            archetype="researcher",
            success_criteria="Relevant information about the order found and summarized.",
            depends_on=[],
            order_index=0,
        )
    ]


async def run(
    refined_request: str,
    available_archetypes: list[dict],
    *,
    model: str,
    user_id: str,
    token: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> list[SubTaskSpec]:
    """Decompose refined_request into a validated SubTaskSpec DAG.

    Args:
        refined_request:      Planner output / user-confirmed triage text.
        available_archetypes: [{id, name, description}] from archetypes.list_archetype_summaries().
        model:                LLM model (determines backend + lane + generation constraint).
        user_id:              Paperless username.
        token:                Paperless session token.
        system_prompt:        Override for Settings integration (Phase 7).

    Returns:
        List of SubTaskSpec in topological order (parents before children).
        Falls back to a single-researcher sub-task if all retries fail.
    """
    from werkbank.llm_lane import complete_structured

    if system_prompt == DEFAULT_SYSTEM_PROMPT:
        from werkbank.settings_store import PROMPT_SPLITTER, get_prompt

        system_prompt = get_prompt(PROMPT_SPLITTER, DEFAULT_SYSTEM_PROMPT)
    archetype_names = {a["name"] for a in available_archetypes}
    arch_list_text = "\n".join(
        f"- {a['name']}: {a['description']}" for a in available_archetypes
    )
    base_user_content = (
        f"Available archetypes:\n{arch_list_text}\n\n"
        f"Work order:\n{refined_request}"
        f"{await _archive_hint(refined_request)}"
    )

    last_error: str | None = None

    for attempt in range(_RETRY_CAP + 1):
        user_content = base_user_content
        if last_error and attempt > 0:
            user_content = (
                f"{base_user_content}\n\n"
                f"--- PREVIOUS ATTEMPT FAILED (attempt {attempt}) ---\n"
                f"Error: {last_error}\n"
                f"Please fix the problem and respond with a valid structure."
            )

        try:
            from werkbank.settings_store import get_tokens, TOKENS_SPLITTER
            raw = await complete_structured(
                system_prompt,
                [{"role": "user", "content": user_content}],
                model=model,
                user_id=user_id,
                token=token,
                json_schema=SUBTASK_JSON_SCHEMA,
                tool_name=_TOOL_NAME,
                max_tokens=get_tokens(TOKENS_SPLITTER),
                temperature=0.1,
            )
            specs = _validate_specs(raw.get("subtasks"), archetype_names)
            # One bounded self-critique expansion pass — no recursion
            specs = await _self_critique_pass(
                specs, refined_request, archetype_names,
                model=model, user_id=user_id, token=token,
            )
            return specs

        except (SplitterParseError, ValueError) as exc:
            last_error = str(exc)
            print(
                f"[splitter] attempt {attempt + 1}/{_RETRY_CAP + 1} failed: {last_error}"
            )

    print("[splitter] All retries exhausted — using single-subtask fallback.")
    return _fallback_spec(refined_request)


# ── DB remapping ──────────────────────────────────────────────────────────────


def insert_subtasks(
    task_id: int,
    user_id: str,
    specs: list[SubTaskSpec],
    archetype_map: dict[str, int],
) -> list[repository.SubTask]:
    """Insert subtasks in topological order, remap temp refs → DB IDs.

    Args:
        task_id:       agent_tasks.id this batch belongs to.
        user_id:       Used for both repository calls and defence-in-depth.
        specs:         Topologically sorted SubTaskSpec list from run().
        archetype_map: {archetype_name: archetype_id} for the user.

    Returns:
        List of fully-inserted SubTask rows (depends_on contains real DB IDs).
    """
    ref_to_id: dict[str, int] = {}
    inserted: list[repository.SubTask] = []

    # First pass — insert in topo order (parents before children)
    for spec in specs:
        st = repository.insert_subtask(
            task_id=task_id,
            user_id=user_id,
            instruction=spec.instruction,
            success_criteria=spec.success_criteria,
            archetype_id=archetype_map.get(spec.archetype),
            order_index=spec.order_index,
        )
        ref_to_id[spec.ref] = st.id
        inserted.append(st)

    # Second pass — write real DB IDs into depends_on
    for spec, st in zip(specs, inserted):
        db_deps = [ref_to_id[r] for r in spec.depends_on if r in ref_to_id]
        if db_deps:
            repository.update_depends_on(st.id, db_deps)

    return inserted

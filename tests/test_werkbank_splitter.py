"""Werkbank splitter — the 8-stage validation of LLM-produced sub-task plans.

This is the guard between a local model's JSON and the orchestrator. The
generation constraint (Ollama `format` / Claude tool-use) enforces shape but not
meaning: a model can still emit duplicate refs, dangle a dependency, or build a
cycle. Those must be caught here, because the orchestrator's ready-set walk
assumes a valid DAG and would otherwise hang or crash mid-run.
"""

import pytest

from werkbank.roles.splitter import (
    MAX_SUBTASKS,
    SplitterParseError,
    _topological_sort,
    _validate_specs,
)

ARCHETYPES = {"retriever", "researcher"}


def _task(ref, deps=None, archetype="retriever"):
    return {
        "ref": ref,
        "instruction": f"do {ref}",
        "archetype": archetype,
        "success_criteria": "done",
        "depends_on": deps or [],
    }


# ── shape ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, {}, "subtasks", 42, []])
def test_non_list_or_empty_input_is_rejected(bad):
    with pytest.raises(SplitterParseError):
        _validate_specs(bad, ARCHETYPES)


def test_too_many_subtasks_is_rejected():
    many = [_task(f"t{i}") for i in range(MAX_SUBTASKS + 1)]
    with pytest.raises(SplitterParseError, match="Too many"):
        _validate_specs(many, ARCHETYPES)


def test_max_subtasks_exactly_is_accepted():
    ok = [_task(f"t{i}") for i in range(MAX_SUBTASKS)]
    assert len(_validate_specs(ok, ARCHETYPES)) == MAX_SUBTASKS


def test_non_object_subtask_is_rejected():
    with pytest.raises(SplitterParseError, match="not an object"):
        _validate_specs(["just a string"], ARCHETYPES)


@pytest.mark.parametrize(
    "field", ["ref", "instruction", "archetype", "success_criteria", "depends_on"]
)
def test_missing_required_field_is_rejected(field):
    task = _task("a")
    del task[field]
    with pytest.raises(SplitterParseError, match="missing fields"):
        _validate_specs([task], ARCHETYPES)


@pytest.mark.parametrize("value", ["", "   ", 42, None])
def test_blank_or_non_string_ref_is_rejected(value):
    task = _task("a")
    task["ref"] = value
    with pytest.raises(SplitterParseError, match="ref"):
        _validate_specs([task], ARCHETYPES)


@pytest.mark.parametrize("value", ["", "   ", 42, None])
def test_blank_instruction_is_rejected(value):
    task = _task("a")
    task["instruction"] = value
    with pytest.raises(SplitterParseError, match="instruction"):
        _validate_specs([task], ARCHETYPES)


def test_depends_on_must_be_a_list():
    task = _task("a")
    task["depends_on"] = "b"
    with pytest.raises(SplitterParseError, match="depends_on"):
        _validate_specs([task], ARCHETYPES)


# ── semantics ────────────────────────────────────────────────────────────────


def test_duplicate_refs_are_rejected():
    with pytest.raises(SplitterParseError, match="Duplicate"):
        _validate_specs([_task("a"), _task("a")], ARCHETYPES)


def test_self_dependency_is_rejected():
    with pytest.raises(SplitterParseError, match="itself"):
        _validate_specs([_task("a", deps=["a"])], ARCHETYPES)


def test_dangling_dependency_is_rejected():
    with pytest.raises(SplitterParseError, match="unknown refs"):
        _validate_specs([_task("a", deps=["ghost"])], ARCHETYPES)


def test_cycle_is_rejected():
    """A cycle would leave the ready-set walk with no runnable task, forever."""
    with pytest.raises(SplitterParseError):
        _validate_specs([_task("a", deps=["b"]), _task("b", deps=["a"])], ARCHETYPES)


def test_longer_cycle_is_rejected():
    with pytest.raises(SplitterParseError):
        _validate_specs(
            [_task("a", deps=["c"]), _task("b", deps=["a"]), _task("c", deps=["b"])],
            ARCHETYPES,
        )


def test_unknown_archetype_falls_back_instead_of_failing():
    """A wrong archetype name is the model being sloppy, not a broken plan —
    degrading to retriever keeps the run alive."""
    specs = _validate_specs([_task("a", archetype="wizard")], ARCHETYPES)
    assert specs[0].archetype == "retriever"


def test_refs_and_instructions_are_stripped():
    task = _task("a")
    task["ref"] = "  a  "
    task["instruction"] = "  do the thing  "
    spec = _validate_specs([task], ARCHETYPES)[0]
    assert spec.ref == "a"
    assert spec.instruction == "do the thing"


# ── topological order ────────────────────────────────────────────────────────


def test_output_is_topologically_sorted():
    """Dependencies must precede dependents, whatever order the model emitted."""
    specs = _validate_specs(
        [_task("c", deps=["b"]), _task("a"), _task("b", deps=["a"])], ARCHETYPES
    )
    order = [s.ref for s in specs]
    assert order.index("a") < order.index("b") < order.index("c")


def test_independent_tasks_all_survive_sorting():
    specs = _validate_specs([_task("a"), _task("b"), _task("c")], ARCHETYPES)
    assert {s.ref for s in specs} == {"a", "b", "c"}


def test_diamond_dependency_orders_correctly():
    specs = _validate_specs(
        [
            _task("d", deps=["b", "c"]),
            _task("b", deps=["a"]),
            _task("c", deps=["a"]),
            _task("a"),
        ],
        ARCHETYPES,
    )
    order = [s.ref for s in specs]
    assert order[0] == "a"
    assert order[-1] == "d"
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_topological_sort_preserves_every_task():
    specs = _validate_specs(
        [_task("a"), _task("b", deps=["a"]), _task("c", deps=["a"])], ARCHETYPES
    )
    assert len(specs) == 3

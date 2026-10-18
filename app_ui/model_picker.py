"""One dropdown for every place a model is chosen: services as headers, models below.

The registry is a tree (provider → models), and a flat list of model names hides
which service a model runs on — two "qwen3" entries, one local and one on
OpenRouter, look identical. Quasar's select has no option groups, so each
provider becomes an extra, disabled option rendered as a header by the option
slot; the models follow it, indented.

The value of the select is still the model's display name — the same string
every setting stored before providers existed — so nothing that reads a model
choice had to change.

Header options are marked by a leading zero-width space in value and label:
NiceGUI hands Quasar plain ``{value: index, label}`` options, so the label is
the only field the client can tell them apart by. A model name never starts with
one (names are stripped on save).
"""

from __future__ import annotations

from nicegui import context, ui

_HEADER = "​"

# Quasar evaluates these on the client; 8203 is the zero-width space.
_IS_HEADER_JS = "opt => String(opt.label).charCodeAt(0) === 8203"

_OPTION_SLOT = r"""
<q-item v-bind="props.itemProps"
        :class="String(props.opt.label).charCodeAt(0) === 8203 ? 'model-pick-group' : 'model-pick-item'">
  <q-item-section>
    <q-item-label>{{ String(props.opt.label).charCodeAt(0) === 8203 ? String(props.opt.label).slice(1) : props.opt.label }}</q-item-label>
  </q-item-section>
</q-item>
"""

_CSS = """
.model-pick-group {
  min-height: 30px !important; padding-top: 10px !important; padding-bottom: 2px !important;
  font-size: 0.72rem; font-weight: 600; color: var(--c-text-muted);
  opacity: 1 !important; cursor: default !important;
}
.model-pick-group:first-child { padding-top: 4px !important; }
.model-pick-item { padding-left: 28px !important; }
"""


def grouped_options(models: list[dict]) -> dict[str, str]:
    """Select options for ``models`` (flattened registry view), headers included.

    Disabled models are left out. The order is the registry order: providers as
    sorted in Settings, models as sorted within them.
    """
    options: dict[str, str] = {}
    last_provider = None
    for m in models:
        if not m.get("enabled", True) or not m.get("name"):
            continue
        provider = m.get("provider_id") or m.get("provider_name") or ""
        if provider != last_provider:
            header = m.get("provider_name") or ""
            options[f"{_HEADER}{provider}"] = f"{_HEADER}{header}"
            last_provider = provider
        options[m["name"]] = m["name"]
    return options


def is_header(value) -> bool:
    return isinstance(value, str) and value.startswith(_HEADER)


def _ensure_css() -> None:
    client = context.client
    if getattr(client, "_model_picker_css", False):
        return
    ui.add_css(_CSS)
    client._model_picker_css = True


def model_select(
    models: list[dict],
    value: str | None = None,
    *,
    label: str | None = None,
    on_change=None,
    allow_empty: bool = False,
    empty_label: str = "",
) -> ui.select:
    """A model dropdown grouped by provider.

    ``value`` is a model display name; one that no longer exists is shown as
    empty rather than kept as an invisible choice. ``allow_empty`` adds an
    entry with value ``""`` at the top (for "no model" settings).
    """
    _ensure_css()
    options = grouped_options(models)
    if allow_empty:
        options = {"": empty_label or "—", **options}
    current = value if value in options and not is_header(value) else (None if not allow_empty else "")
    sel = ui.select(options, value=current, label=label, on_change=on_change)
    sel.props(f':option-disable="{_IS_HEADER_JS}"')
    sel.add_slot("option", _OPTION_SLOT)
    return sel

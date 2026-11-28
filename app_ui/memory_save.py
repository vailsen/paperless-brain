"""Save part of a chat answer as a memory (the answer's "…" menu, next to
"Save to dossier").

A memory is one fact in the assistant's own long-term memory — the same file,
index and identity the model's `remember_fact` tool writes
(`vault_brain_writer.create_memory`). Nothing is saved before the user saw and
could edit the text; a fact that is nearly the same as one already stored is
shown first, with "Save anyway".

The text starts as the selected part of the answer, else the whole answer in
plain form: reference markers become readable references (`[#241 p3]`), web
sources plain links. When everything cited comes from one document, that
document (and its page, if only one) is kept as the fact's source.
"""

from __future__ import annotations

from nicegui import ui

from i18n import get_translator


def memory_text(answer: str, selection: str = "") -> str:
    """What the dialog starts with: the selection, else the plain answer."""
    from services.dossier_merge import plain_refs
    from services.web_citations import plain_web_refs

    text = (selection or "").strip() or (answer or "").strip()
    return plain_refs(plain_web_refs(text)).strip()


def single_source(text: str) -> tuple[int | None, int | None]:
    """(document id, page) when every citation in ``text`` names one document;
    the page only when it is the same everywhere."""
    from services.citations import CITE_RE

    found = [(int(m.group(1)), int(m.group(2)) if m.group(2) else None) for m in CITE_RE.finditer(text or "")]
    ids = {d for d, _p in found}
    if len(ids) != 1:
        return None, None
    pages = {p for _d, p in found}
    return next(iter(ids)), (next(iter(pages)) if len(pages) == 1 else None)


def parse_tags(raw: str) -> list[str]:
    return [t.strip().lstrip("#") for t in (raw or "").replace(";", ",").split(",") if t.strip().lstrip("#")]


def clean_keywords(values) -> list[str]:
    """The picked keywords: trimmed, no "#", each once (case-insensitive)."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        for t in parse_tags(str(v)):
            if t.casefold() not in seen:
                seen.add(t.casefold())
                out.append(t)
    return out


def known_keywords(tag_lists: list[list[str]]) -> list[str]:
    """Every keyword the user's memories carry, most used first — spelled as
    most often written ("Insurance" and "insurance" are one keyword)."""
    from collections import Counter

    count: Counter = Counter()
    spelling: dict[str, Counter] = {}
    for tags in tag_lists:
        for t in clean_keywords(tags):
            k = t.casefold()
            count[k] += 1
            spelling.setdefault(k, Counter())[t] += 1
    return [spelling[k].most_common(1)[0][0] for k, _n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))]


def suggest_keywords(known: list[str], text: str, limit: int = 8) -> list[str]:
    """Keywords to offer as one tap: those whose words occur in the memory's
    text first, then the most used ones."""
    import re

    words = {w.casefold() for w in re.findall(r"\w{3,}", text or "")}

    def _in_text(k: str) -> bool:
        parts = [w.casefold() for w in re.findall(r"\w{3,}", k)]
        return bool(parts) and all(any(w.startswith(p[:5]) for w in words) for p in parts)

    hits = [k for k in known if _in_text(k)]
    rest = [k for k in known if k not in hits]
    return (hits + rest)[:limit]


async def open_memory_dialog(*, user: str, answer: str, selection: str = "") -> None:
    _ = get_translator()
    if not user:
        ui.notify(_("No user signed in."), type="warning")
        return
    src_doc, src_page = single_source(selection or answer)
    with ui.dialog() as dlg, ui.card().classes("gap-2").style(
            "background:var(--c-surface); width:520px; max-width:94vw;"):
        ui.label(_("Save as memory")).classes("text-sm font-semibold").style("color:var(--c-text)")
        ui.label(_("The assistant's own long-term memory: it recalls this in later chats. "
                   "One clear fact per memory works best — shorten it if you like.")).classes(
            "text-xs").style("color:var(--c-text-muted)")
        area = ui.textarea(value=memory_text(answer, selection)).props(
            "outlined autogrow autofocus").classes("w-full").style("max-height:50vh; overflow:auto")
        # Keywords of this memory (its `tags:` property) — not Paperless tags.
        # Picked from the ones the user's memories already carry, or typed new.
        from services.clients import brain

        try:
            known = known_keywords([f.tags for f in await brain.get_all(user) if f.user == user])
        except Exception:  # noqa: BLE001 — no index: no suggestions, typing still works
            known = []
        tags = ui.select(known, multiple=True, with_input=True, new_value_mode="add-unique",
                         label=_("Keywords (optional)")).props(
            "outlined dense use-chips hide-dropdown-icon input-debounce=0").classes("w-full")
        tags.tooltip(_("Type a keyword and press Enter, or pick one you used before"))
        sugg_row = ui.row().classes("w-full items-center gap-1").style("margin-top:-4px")

        def _suggest() -> None:
            sugg_row.clear()
            picked = {t.casefold() for t in clean_keywords(tags.value)}
            offer = [k for k in suggest_keywords(known, area.value or "", limit=12) if k.casefold() not in picked][:8]
            if not offer:
                return
            with sugg_row:
                ui.label(_("Suggested:")).classes("text-xs").style("color:var(--c-text-muted)")
                for k in offer:
                    ui.chip(k, icon="add", on_click=lambda k=k: _add(k)).props("dense outline clickable").classes(
                        "text-xs")

        def _add(k: str) -> None:
            tags.set_value(clean_keywords([*(tags.value or []), k]))

        tags.on_value_change(lambda _e: _suggest())
        area.on("blur", lambda _e: _suggest())
        _suggest()
        if src_doc:
            ui.label(_("Source: document #{id}").format(id=src_doc)
                     + (" · " + _("page {n}").format(n=src_page) if src_page else "")).classes(
                "text-xs").style("color:var(--c-text-muted)")
        similar_box = ui.column().classes("w-full gap-1")
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
            save_btn = ui.button(_("Save")).props("unelevated no-caps color=primary")
    state = {"force": False}

    async def _save() -> None:
        from services.chat_service import _emb_threshold
        from services.clients import brain, vault_brain_writer

        text = (area.value or "").strip()
        if not text:
            ui.notify(_("Nothing to save."), type="warning")
            return
        save_btn.props(add="loading")
        try:
            if not state["force"]:
                hits = await brain.search_hints(text, user, max_results=3)
                dup = _emb_threshold("fact_duplicate", 0.15)
                close = [(t, d) for _fid, t, d in hits if d < dup]
                if close:
                    similar_box.clear()
                    with similar_box:
                        ui.label(_("Very similar memories already exist:")).classes("text-xs").style(
                            "color:var(--c-warn)")
                        for t, d in close:
                            ui.label(f"{1 - d:.0%} · {t}").classes("text-xs w-full").style(
                                "color:var(--c-text-2); overflow-wrap:anywhere")
                    state["force"] = True
                    save_btn.set_text(_("Save anyway"))
                    return
            await vault_brain_writer.create_memory(
                text=text, tags=clean_keywords(tags.value), user=user,
                source_doc_id=src_doc, source_page=src_page,
            )
        except Exception as exc:  # noqa: BLE001 — say why; nothing was half-written by the UI
            ui.notify(_("Could not save the memory: {err}").format(err=exc), type="negative")
            return
        finally:
            save_btn.props(remove="loading")
        dlg.close()
        ui.notify(_("Saved to memory."), type="positive")

    save_btn.on_click(_save)
    dlg.open()
    await dlg
    dlg.delete()

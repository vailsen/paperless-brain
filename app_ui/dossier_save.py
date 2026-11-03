"""The "Save to dossier" dialog on chat answers (docs/findings-workspace.md §5).

Pick a dossier (or make one) → the merge round fits the excerpt into it →
a preview the user can edit, with what the checks found → save. Nothing is
written before the user presses Save. "Save verbatim" skips the model.

Provenance is the turn's, computed by code (`services/dossier_merge.provenance`)
and passed in; the dialog shows it and never lets the model touch it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from nicegui import ui

from i18n import get_translator

log = logging.getLogger("dossier_save")

_NEW = "__new__"


def _evidence_label(_, kind: str) -> str:
    return {
        "documents": _("documents"), "web": _("web"), "documents+web": _("documents + web"),
        "reasoning": _("reasoning only — no tool evidence"),
    }.get(kind, kind)


async def open_save_dialog(
    *, user: str, token: str, answer: str, selection: str, provenance: dict, conv_id: str,
    msg_index: int, preselect: str, models: list[dict], default_model: str,
    on_saved: Callable[[str], None] | None = None,
    conversation: list[dict] | None = None, conv_provenance: dict | None = None,
) -> None:
    from app_ui.model_picker import model_select
    from proactive import dossiers
    from services import dossier_merge
    from services.user_prefs import get_pref, set_pref

    _ = get_translator()
    one = {"excerpt": (selection or answer or "").strip(), "prov": provenance or {}}
    whole = None
    if conversation:
        t = dossier_merge.transcript(conversation)
        if t and sum(1 for m in conversation if m.get("role") == "assistant") > 1:
            whole = {"excerpt": t, "prov": conv_provenance or {}}
    cur = {"mode": "answer"}

    def _excerpt() -> str:
        return (whole if cur["mode"] == "conversation" and whole else one)["excerpt"]

    def _prov() -> dict:
        return (whole if cur["mode"] == "conversation" and whole else one)["prov"]
    all_d = sorted(dossiers.list_dossiers(user), key=lambda d: (d.folder.casefold(), d.display.casefold()))
    options = {d.topic: (f"{d.folder} / {d.display}" if d.folder else d.display) for d in all_d}
    options[_NEW] = _("New dossier …")
    start = preselect if preselect in options else (next(iter(options)) if len(options) > 1 else _NEW)
    model0 = get_pref("dossier_merge_model", "") or default_model
    state: dict = {"result": None, "busy": False}

    with ui.dialog() as dlg, ui.card().style(
        "background:var(--c-surface); width:min(680px,96vw); max-width:96vw; max-height:90vh;"
        "overflow-y:auto; border:1px solid var(--c-border); gap:10px;"
    ):
        with ui.row().classes("w-full items-center no-wrap"):
            ui.icon("create_new_folder", size="sm").classes("text-gray-400")
            ui.label(_("Save to dossier")).classes("text-base font-semibold flex-1").style("color:var(--c-text)")
            ui.button(icon="close", on_click=dlg.close).props("flat dense round").classes("card-action-btn")

        target = ui.select(options, value=start, label=_("Dossier")).props("outlined dense options-dense").classes("w-full")
        new_name = ui.input(_("Name of the new dossier")).props("outlined dense").classes("w-full")
        new_name.set_visibility(start == _NEW)
        target.on_value_change(lambda e: new_name.set_visibility(e.value == _NEW))

        if whole:
            ui.toggle({"answer": _("This answer") if not selection else _("The selected text"),
                       "conversation": _("The whole conversation")}, value="answer",
                      on_change=lambda e: _mode(e.value)).props("dense no-caps unelevated").classes("self-start")
        src_host = ui.column().classes("w-full gap-2")

        note = ui.input(_("Your note for the merge (optional)")).props("outlined dense").classes("w-full")
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            msel = model_select(models, model0 if any(m["name"] == model0 for m in models) else default_model,
                                label=_("Model for fitting it in")).props("outlined dense").classes("flex-1")
        status = ui.label("").classes("text-xs").style("color:var(--c-text-muted)")

        preview = ui.column().classes("w-full gap-2")

        with ui.row().classes("w-full justify-end gap-2"):
            verbatim_btn = ui.button(_("Save verbatim"), icon="content_paste").props("flat no-caps dense")
            merge_btn = ui.button(_("Fit into dossier"), icon="auto_awesome").props(
                "unelevated no-caps dense").classes("bg-primary text-white")
            save_btn = ui.button(_("Save"), icon="save").props("unelevated no-caps dense").classes("bg-primary text-white")
            save_btn.set_visibility(False)

    def _render_src() -> None:
        excerpt, provenance = _excerpt(), _prov()
        src_host.clear()
        with src_host:
            with ui.expansion(_("Excerpt ({n} characters)").format(n=len(excerpt)),
                              icon="format_quote", value=bool(selection) and cur["mode"] == "answer").classes(
                    "w-full").props("dense"):
                ui.markdown(excerpt[:6000]).classes("chat-md text-sm").style("color:var(--c-text-2)")
            # Provenance, from the tool log — shown, never edited.
            with ui.column().classes("w-full gap-1").style(
                    "border:1px solid var(--c-border); border-radius:8px; padding:8px 10px;"):
                ui.label(_("Sources (from this answer's tool calls)") if cur["mode"] == "answer"
                         else _("Sources (from the tool calls of every answer)")).classes(
                    "text-xs font-semibold").style("color:var(--c-text-2)")
                if not provenance:
                    ui.label(_("No tool log stored for this answer — sources unknown.")).classes("text-xs").style(
                        "color:var(--c-text-muted)")
                else:
                    ui.label(_("Evidence: {kind}").format(kind=_evidence_label(_, provenance.get("evidence", "")))).classes(
                        "text-xs").style("color:var(--c-text-muted)")
                    if provenance.get("docs"):
                        ui.label(_("Documents: {list}").format(list=", ".join(
                            f"#{d['id']}" + (f" p{d['page']}" if d.get("page") else "") for d in provenance["docs"]))).classes(
                            "text-xs").style("color:var(--c-text-muted)")
                    for w in provenance.get("web") or []:
                        ui.label(w["url"]).classes("text-xs").style(
                            "color:var(--c-text-muted); overflow-wrap:anywhere;")

    def _mode(m: str) -> None:
        cur["mode"] = m
        state["result"] = None
        preview.clear()
        save_btn.set_visibility(False)
        merge_btn.props(remove="flat").classes(add="bg-primary text-white")
        verbatim_btn.set_visibility(m == "answer")
        _render_src()

    def _dossier():
        topic = target.value
        if topic == _NEW:
            return None
        return dossiers.load(user, topic)

    def _show_preview(res: dict, d, merged_by: str) -> None:
        state["result"] = dict(res, merged_by=merged_by)
        preview.clear()
        with preview:
            ui.separator()
            ui.label(_("Preview — edit before saving")).classes("text-xs font-semibold").style("color:var(--c-text-2)")
            state["title"] = ui.input(_("Title"), value=res["title"]).props("outlined dense").classes("w-full")
            state["text"] = ui.textarea(_("Insight"), value=res["text"]).props("outlined autogrow").classes("w-full")
            if res.get("unsupported"):
                with ui.row().classes("w-full items-start gap-2 no-wrap").style(
                    "background:var(--c-warn-bg); border-radius:8px; padding:6px 8px;"):
                    ui.icon("warning", size="xs").style("color:var(--c-warn)")
                    ui.label(_("Not found in the answer or its sources: {n}. Check before saving.").format(
                        n=", ".join(res["unsupported"]))).classes("text-xs").style("color:var(--c-text)")
            if res.get("contradiction"):
                with ui.row().classes("w-full items-start gap-2 no-wrap"):
                    ui.icon("compare_arrows", size="xs").classes("text-gray-400")
                    ui.label(_("Differs from the current state: {t}").format(t=res["contradiction"])).classes(
                        "text-xs").style("color:var(--c-text-2)")
            state["answers"] = []
            if d is not None and d.open_questions:
                ui.label(_("Open questions this answers")).classes("text-xs font-semibold").style("color:var(--c-text-2)")
                for i, q in enumerate(d.open_questions, 1):
                    cb = ui.checkbox(q, value=i in (res.get("answers") or [])).props("dense").classes("text-sm")
                    state["answers"].append((q, cb))
            state["new_q"] = []
            if res.get("new_questions"):
                ui.label(_("New open questions")).classes("text-xs font-semibold").style("color:var(--c-text-2)")
                for q in res["new_questions"]:
                    state["new_q"].append((q, ui.checkbox(q, value=True).props("dense").classes("text-sm")))
        save_btn.set_visibility(True)
        merge_btn.props("flat").classes(remove="bg-primary text-white")

    async def _merge() -> None:
        if state["busy"]:
            return
        d = _dossier()
        if target.value == _NEW and not (new_name.value or "").strip():
            ui.notify(_("Name the new dossier first."), type="warning")
            return
        model = msel.value or default_model
        if not model:
            ui.notify(_("No model configured — add one in Settings > AI Models."), type="negative")
            return
        set_pref("dossier_merge_model", model)
        state["busy"] = True
        status.set_text(_("Fitting it into the dossier …"))
        merge_btn.props("loading")
        try:
            fake = d or dossiers.Dossier(topic="", label=(new_name.value or "").strip(),
                                         lang=get_pref("language", "en"))
            res = await dossier_merge.merge(
                _excerpt(), fake, model=model, user=user, token=token,
                haystack=str(_prov().get("haystack") or ""), note=note.value or "",
                conversation=cur["mode"] == "conversation",
            )
            _show_preview(res, d, model)
            status.set_text(_("The model answered as plain text; numbers and questions were checked the same way.")
                            if res.get("mode") == "text" else "")
        except Exception as exc:
            log.warning("merge round failed", exc_info=True)
            from werkbank.v2.pipeline import describe_failure

            kind, _detail = describe_failure(exc)
            why = _("the model host does not answer") if kind == "host" else (str(exc) or type(exc).__name__)
            status.set_text(_("The model could not do it: {err}. You can save the excerpt verbatim.").format(
                err=why[:300]))
        finally:
            state["busy"] = False
            merge_btn.props(remove="loading")

    def _verbatim() -> None:
        _show_preview(dossier_merge.verbatim(_excerpt()), _dossier(), "")

    async def _save() -> None:
        res = state.get("result")
        if not res:
            return
        title = " ".join((state["title"].value or "").split())[:dossier_merge.MAX_TITLE]
        text = (state["text"].value or "").strip()
        if not title or not text:
            ui.notify(_("Title and text are needed."), type="warning")
            return
        excerpt, provenance = _excerpt(), _prov()
        try:
            topic = target.value
            if topic == _NEW:
                lang = get_pref("language", "en")
                d = await dossiers.create_manual(user, new_name.value or "", lang=lang)
                topic = d.topic
            from datetime import datetime

            ins = dossiers.Insight(
                insight_id="", created=datetime.now().isoformat(timespec="minutes"),
                title=title, text=dossier_merge.check_merge(
                    {"title": title, "insight": text}, excerpt=excerpt, haystack="", n_questions=0)["text"],
                evidence=str(provenance.get("evidence") or "reasoning"), conv_id=conv_id, msg_index=msg_index,
                model=str(provenance.get("model") or ""), expert=str(provenance.get("expert") or ""),
                docs=list(provenance.get("docs") or []), web=list(provenance.get("web") or []),
                merged_by=res.get("merged_by", ""),
                unsupported=dossier_merge.numbers_missing(
                    f"{title} {text}", f"{excerpt}\n{provenance.get('haystack') or ''}"),
            )
            answered = [q for q, cb in state.get("answers", []) if cb.value]
            ins.answers = answered
            new_q = [q for q, cb in state.get("new_q", []) if cb.value]
            await dossiers.add_insight(user, topic, ins, answered=answered, new_questions=new_q)
        except dossiers.DossierError as exc:
            msg = {"exists": _("A dossier with that name already exists."),
                   "name": _("Give the dossier a name.")}.get(str(exc), _("Could not save."))
            ui.notify(msg, type="warning")
            return
        except Exception as exc:
            log.warning("saving insight failed", exc_info=True)
            ui.notify(_("Could not save: {err}").format(err=exc), type="negative")
            return
        dlg.close()
        ui.notify(_("Saved to dossier."), type="positive")
        if on_saved:
            on_saved(topic)

    _render_src()
    merge_btn.on_click(_merge)
    verbatim_btn.on_click(_verbatim)
    save_btn.on_click(_save)
    dlg.open()

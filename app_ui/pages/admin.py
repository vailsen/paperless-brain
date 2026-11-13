"""/admin — settings that apply to every user, for the names in `ADMIN_USERS`.

For now: the embedding models and their indexes (`services/embedding_registry`,
`services/embedding_index`). Build a second index from the stored chunks,
compare the two by semantic search (documents and timings — no LLM), make one
active, delete the other or keep it dormant.
"""

from __future__ import annotations

import asyncio
import logging
import statistics

from nicegui import app as ng_app, ui

from app_ui.layout import page_layout, require_auth
from i18n import get_translator
from services import embedding_index as ei
from services import embedding_registry as reg
from services import embeddings as emb
from services.admin import is_admin

log = logging.getLogger(__name__)

_CSS = """<style>
.adm-wrap { max-width: 980px; margin: 0 auto; width: 100%; }
.adm-ellipsis { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.adm-mono { font-family: monospace; overflow-wrap:anywhere; word-break:break-all; }
.adm-card { background: var(--c-surface); border: 1px solid var(--c-border); border-radius: 10px; }
.adm-badge { font-size: 11px; padding: 1px 6px; border-radius: 6px; white-space: nowrap;
             color: var(--c-text-2); border: 1px solid var(--c-border); }
.adm-badge.is-active { color: var(--c-accent-text); border-color: var(--c-accent-border);
                       background: var(--c-accent-soft); }
.adm-warn { color: var(--c-warn); background: var(--c-warn-bg); border-radius: 6px; }
.adm-cmp { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; width: 100%; }
@media (max-width: 767px) { .adm-cmp { grid-template-columns: 1fr; } }
.adm-doc { width: 100%; display: grid; grid-template-columns: 22px 1fr auto; gap: 6px; align-items: baseline;
           font-size: 13px; padding: 2px 0; min-width: 0; }
.adm-doc .t { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; min-width:0;
              color: var(--c-text); }
.adm-doc .d { font-family: monospace; font-size: 12px; color: var(--c-text-muted); }
.adm-doc.only .t { font-weight: 600; }
.adm-num { font-family: monospace; color: var(--c-text-muted); font-size: 12px; text-align: right; }
</style>"""


def _fmt_s(secs: float) -> str:
    secs = int(max(secs, 0))
    if secs < 90:
        return f"{secs} s"
    if secs < 5400:
        return f"{round(secs / 60)} min"
    return f"{secs / 3600:.1f} h"


@ui.page("/admin")
async def admin_page():
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html(_CSS)
    user: str = ng_app.storage.user.get("paperless_user", "")
    if not is_admin(user):
        with ui.column().classes("adm-wrap p-4"):
            ui.label(_("Admins only")).classes("text-lg font-semibold text-gray-100")
            ui.label(_("The admin area is open to the users named in ADMIN_USERS on the server.")).classes(
                "text-sm text-gray-400")
        return

    state_label = {
        "none": _("No index"),
        "building": _("Building"),
        "paused": _("Incomplete"),
        "ready": _("Ready"),
    }
    role_label = {"documents": _("Documents"), "brain": _("Memory"), "vault": _("Notes")}
    th_label = {"archive_hint": _("archive hint"), "fact_duplicate": _("similar fact"),
                "brain_hint": _("memory hint")}

    with ui.column().classes("adm-wrap p-3 gap-3"):
        with ui.column().classes("gap-0"):
            ui.label(_("Admin")).classes("text-xl font-semibold text-gray-100")
            ui.label(_("Settings for every user. Admins are named in ADMIN_USERS on the server.")).classes(
                "text-xs text-gray-500")

        # ── Embedding models ─────────────────────────────────────────────────
        with ui.row().classes("w-full items-center justify-between no-wrap mt-2"):
            with ui.row().classes("items-center gap-3 no-wrap min-w-0"):
                ui.icon("hub", size="sm").classes("text-gray-400")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(_("Embedding models")).classes("text-base font-semibold text-gray-100")
                    ui.label(_("Search reads the active model's index, for every user")).classes(
                        "text-xs text-gray-500")
            ui.button(_("Model"), icon="add", on_click=lambda: _model_dialog(None)).props(
                "unelevated dark dense").classes("bg-primary text-white flex-shrink-0")

        counts: dict[str, dict] = {}

        async def _load_counts() -> None:
            for m in reg.models():
                if m.get("index") != "none":
                    counts[m["id"]] = await ei.counts(m)
            model_list.refresh()

        def _menu(entries) -> None:
            with ui.button(icon="more_vert", color=None).props("flat dense round").classes(
                    "flex-shrink-0").style("color:var(--c-text-2)"):
                with ui.menu().props("auto-close"):
                    for icon, label, handler, enabled in entries:
                        with ui.menu_item(on_click=handler).props("" if enabled else "disable").classes(
                                "items-center gap-3"):
                            ui.icon(icon, size="xs").style("color:var(--c-text-2)")
                            ui.label(label)

        _progress_views: list = []

        @ui.refreshable
        def model_list() -> None:
            _progress_views.clear()
            active = reg.active_id()
            job = ei.current_job()
            loaded = emb.loaded_models()
            for m in sorted(reg.models(), key=lambda x: x["id"] != active):
                mid, is_active = m["id"], m["id"] == active
                idx = m.get("index", "none")
                running = job is not None and job.model_id == mid
                p = m.get("progress") or {}
                with ui.column().classes("w-full adm-card px-3 py-2 gap-1"):
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                            ui.label(m["name"]).classes("w-full text-sm font-semibold text-gray-100 adm-ellipsis")
                            with ui.row().classes("items-center gap-1 mt-0.5"):
                                if is_active:
                                    ui.label(_("Active")).classes("adm-badge is-active")
                                elif idx == "ready":
                                    ui.label(_("Dormant")).classes("adm-badge")
                                else:
                                    ui.label(state_label.get(idx, idx)).classes("adm-badge")
                                ui.label(_("on this server") if m.get("use") == "local" else _("remote")).classes(
                                    "adm-badge")
                                if m["name"] in loaded:
                                    ui.label(_("in RAM")).classes("adm-badge")
                        _menu([
                            ("edit", _("Edit"), lambda x=m: _model_dialog(x), True),
                            ("construction",
                             _("Continue building") if idx == "paused" else
                             (_("Catch up") if idx == "ready" else _("Build index")),
                             lambda x=mid: _start(x, "build"), not is_active and job is None),
                            ("stop_circle", _("Stop"), lambda: (ei.stop(), model_list.refresh()), running),
                            ("check_circle", _("Make active"), lambda x=m: _confirm_activate(x),
                             not is_active and job is None and idx in ("ready", "paused")),
                            ("swap_horiz",
                             _("Embed remotely") if m.get("use") == "local" else _("Embed on this server"),
                             lambda x=m: _switch_use(x), reg.has_remote(m) and not running),
                            ("compare_arrows", _("Check local against remote"), lambda x=mid: _parity(x),
                             reg.has_remote(m) and job is None),
                            ("tune", _("Distance thresholds"), lambda x=m: _thresholds_dialog(x), True),
                            ("delete_sweep", _("Delete index"), lambda x=m: _confirm_delete_index(x),
                             not is_active and idx != "none" and not running),
                            ("delete", _("Remove model"), lambda x=mid: _remove(x),
                             not is_active and idx == "none"),
                        ])
                    if m.get("use") == "remote":
                        ui.label(f"{m['remote_url']} · {m['remote_model']}").classes("w-full text-xs text-gray-500 adm-mono")
                    c = counts.get(mid)
                    if c and idx != "none":
                        ui.label(" · ".join(f"{role_label[r]} {c.get(r, 0)}" for r in reg.ROLES)).classes(
                            "text-xs text-gray-500")
                    if running or (idx in ("building", "paused") and p.get("total")):
                        # Only this part redraws while a job runs — redrawing the
                        # whole list closed an open menu every two seconds.
                        @ui.refreshable
                        def progress(model_id=mid, show_stop=running) -> None:
                            q = (reg.get(model_id) or {}).get("progress") or {}
                            done, total = int(q.get("done") or 0), int(q.get("total") or 0)
                            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                                ui.label(_("{part}: {done} of {total} · {n} embedded").format(
                                    part=role_label.get(q.get("role", ""), q.get("role", "")), done=done,
                                    total=total, n=q.get("embedded", 0))).classes(
                                    "flex-1 min-w-0 text-xs text-gray-400")
                                if show_stop:
                                    ui.button(_("Stop"), icon="stop_circle", color=None,
                                              on_click=lambda: (ei.stop(), ui.notify(_("Stops after this batch.")))
                                              ).props("flat dense no-caps").classes("text-xs").style(
                                        "color:var(--c-text-2)")
                            ui.linear_progress(value=(done / total) if total else 0, show_value=False).props(
                                "color=primary size=4px rounded")

                        progress()
                        if running:
                            _progress_views.append(progress)
                    elif idx == "ready" and not is_active and m.get("synced_at"):
                        ui.label(_("Caught up with the active index: {when}").format(
                            when=m["synced_at"][:16].replace("T", " "))).classes("text-xs text-gray-500")
                    if p.get("finished") and not running and p.get("seconds") is not None:
                        ui.label(_("Last run: {n} embedded in {t}").format(
                            n=p.get("embedded", 0), t=_fmt_s(p["seconds"]))).classes("text-xs text-gray-500")
                    if p.get("error"):
                        ui.label(p["error"]).classes("w-full text-xs px-2 py-1 adm-warn adm-mono")
                    par = m.get("parity") or {}
                    if reg.has_remote(m) and par:
                        same = reg.parity_ok(m)
                        ui.label(_("Local against remote: cosine {mean} (lowest {min}) — {verdict}").format(
                            mean=par.get("mean"), min=par.get("min"),
                            verdict=_("the same model") if same else _("not the same vectors"))).classes(
                            "text-xs " + ("text-gray-500" if same else "px-2 py-1 adm-warn"))
                    if is_active and m.get("use") == "remote":
                        ui.label(_("Every search asks this endpoint; while it is off, search finds nothing.")).classes(
                            "text-xs text-gray-500")
                    th = m.get("thresholds") or {}
                    if th:
                        ui.label(_("Thresholds: {values}").format(
                            values=", ".join(f"{th_label.get(k, k)} {v:g}" for k, v in th.items()))).classes(
                            "text-xs text-gray-500")

        model_list()
        def _tick() -> None:
            if ei.current_job():
                for view in list(_progress_views):
                    view.refresh()

        refresh_timer = ui.timer(2.0, _tick)
        ui.timer(0.1, _load_counts, once=True)

        def _was_running_then_done() -> None:
            # One more refresh (and fresh counts) when a job ends.
            if ei.current_job() is None and refresh_timer.active and _was["job"]:
                _was["job"] = False
                asyncio.ensure_future(_load_counts())
            elif ei.current_job():
                _was["job"] = True

        _was = {"job": bool(ei.current_job())}
        ui.timer(2.0, _was_running_then_done)

        # ── Actions ──────────────────────────────────────────────────────────

        def _start(mid: str, kind: str) -> None:
            try:
                ei.start(mid, kind)
            except Exception as exc:  # noqa: BLE001
                ui.notify(str(exc), type="warning")
                return
            _was["job"] = True
            model_list.refresh()

        def _confirm_activate(m: dict) -> None:
            with ui.dialog() as dlg, ui.card().classes("bg-gray-900").style("width:min(92vw,460px)"):
                ui.label(_("Make \"{name}\" active?").format(name=m["name"])).classes(
                    "text-base font-semibold text-gray-100")
                ui.label(_(
                    "Its index is first brought up to date with the active one, then every search of "
                    "every user reads it. The current index stays as it is, dormant: it is no longer "
                    "updated until it is made active again.")).classes("text-sm text-gray-400")
                with ui.row().classes("w-full justify-end gap-2 mt-3"):
                    ui.button(_("Cancel"), on_click=dlg.close).props("flat dense")

                    def _go() -> None:
                        dlg.close()
                        _start(m["id"], "activate")

                    ui.button(_("Make active"), on_click=_go).props("unelevated dense color=primary")
            dlg.open()

        def _confirm_delete_index(m: dict) -> None:
            with ui.dialog() as dlg, ui.card().classes("bg-gray-900").style("width:min(92vw,460px)"):
                ui.label(_("Delete the index of \"{name}\"?").format(name=m["name"])).classes(
                    "text-base font-semibold text-gray-100")
                ui.label(_(
                    "Its vectors for documents, notes and memory are deleted, and the model's files on "
                    "this server — a remote endpoint is not touched. The documents, notes and "
                    "extractions themselves stay; the index can be built again from the active one.")).classes(
                    "text-sm text-gray-400")
                with ui.row().classes("w-full justify-end gap-2 mt-3"):
                    ui.button(_("Cancel"), on_click=dlg.close).props("flat dense")

                    async def _go() -> None:
                        dlg.close()
                        try:
                            freed = await ei.delete_index(m["id"])
                            if freed:
                                ui.notify(_("Index deleted, and the model's files on this server ({gb} GB).").format(
                                    gb=f"{freed / 1e9:.1f}"), type="info")
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(str(exc), type="warning")
                        counts.pop(m["id"], None)
                        model_list.refresh()
                        compare_box.refresh()

                    ui.button(_("Delete"), on_click=_go).props("unelevated dense color=red")
            dlg.open()

        async def _remove(mid: str) -> None:
            try:
                freed = await ei.remove_model(mid)
                if freed:
                    ui.notify(_("Model removed, and its files on this server ({gb} GB).").format(
                        gb=f"{freed / 1e9:.1f}"), type="info")
            except ValueError as exc:
                ui.notify(str(exc), type="warning")
            model_list.refresh()
            compare_box.refresh()

        def _switch_use(m: dict) -> None:
            use = "remote" if m.get("use") == "local" else "local"
            ok, why = reg.can_switch_use(m, use)
            if not ok:
                ui.notify(_("Run \"Check local against remote\" first: an index must not mix vectors of "
                            "two different models.") if why == "parity" else _("No remote endpoint set."),
                          type="warning", multi_line=True)
                return
            reg.set_use(m["id"], use)
            model_list.refresh()

        async def _parity(mid: str) -> None:
            n = ui.notification(_("Embedding the same texts locally and remotely …"), spinner=True,
                                timeout=None)
            try:
                await ei.parity(mid)
            except Exception as exc:  # noqa: BLE001
                ui.notify(str(exc), type="warning", multi_line=True)
            finally:
                n.dismiss()
            model_list.refresh()

        def _model_dialog(edit: dict | None) -> None:
            with ui.dialog().props("persistent") as dlg, ui.card().classes("bg-gray-900").style(
                    "width:min(94vw,520px)"):
                ui.label(_("Edit embedding model") if edit else _("Add embedding model")).classes(
                    "text-base font-semibold text-gray-100")
                f_name = ui.input(_("Model (Hugging Face id)"), placeholder="Qwen/Qwen3-Embedding-0.6B").props(
                    "outlined dark dense").classes("w-full mt-2")
                if edit:
                    f_name.set_value(edit["name"])
                    f_name.props("readonly")
                ui.label(_("Runs on this server with sentence-transformers; the first use downloads it. "
                           "The index belongs to the model, so the id cannot change.")).classes(
                    "text-xs text-gray-500")
                f_prompt = ui.textarea(_("Query instruction")).props("outlined dark dense autogrow").classes(
                    "w-full mt-2")
                f_prompt.set_value(edit["query_prompt"] if edit else "")
                ui.label(_("Put before every search query, never before stored text. Empty = filled in "
                           "for known models (e5-instruct, Qwen3-Embedding).")).classes("text-xs text-gray-500")
                ui.label(_("Remote endpoint (optional)")).classes("text-sm text-gray-300 mt-3")
                f_url = ui.input(_("Base URL"), placeholder="http://192.168.1.50:11434").props(
                    "outlined dark dense").classes("w-full")
                f_rmodel = ui.input(_("Model name there"), placeholder="qwen3-embedding:0.6b").props(
                    "outlined dark dense").classes("w-full mt-2")
                f_key = ui.input(_("API key"), password=True, password_toggle_button=True).props(
                    "outlined dark dense").classes("w-full mt-2")
                if edit:
                    f_url.set_value(edit.get("remote_url", ""))
                    f_rmodel.set_value(edit.get("remote_model", ""))
                    if edit.get("remote_key"):
                        f_key.props(f'placeholder="{_("stored — empty keeps it")}"')
                ui.label(_("Any OpenAI-compatible /v1/embeddings (Ollama, TEI, vLLM). The same model "
                           "remotely, e.g. to build a large index on a GPU and search it on this "
                           "server afterwards.")).classes("text-xs text-gray-500")

                def _save() -> None:
                    prompt = (f_prompt.value or "").strip("\n")
                    try:
                        if edit:
                            reg.edit_model(edit["id"], query_prompt=prompt or emb.default_query_prompt(edit["name"]),
                                           remote_url=f_url.value or "", remote_model=f_rmodel.value or "",
                                           api_key=(f_key.value or None))
                        else:
                            name = (f_name.value or "").strip()
                            reg.add_model(name, query_prompt=prompt or None, remote_url=f_url.value or "",
                                          remote_model=f_rmodel.value or "", api_key=f_key.value or "")
                    except ValueError as exc:
                        ui.notify(_("This model is already in the list.") if str(exc) == "model exists"
                                  else str(exc), type="warning")
                        return
                    dlg.close()
                    model_list.refresh()
                    compare_box.refresh()

                with ui.row().classes("w-full justify-end gap-2 mt-4"):
                    ui.button(_("Cancel"), on_click=dlg.close).props("flat dark").classes("text-gray-400")
                    ui.button(_("Save"), on_click=_save).props("unelevated dark").classes("bg-primary text-white")
            dlg.open()

        def _thresholds_dialog(m: dict) -> None:
            th = m.get("thresholds") or {}
            with ui.dialog() as dlg, ui.card().classes("bg-gray-900").style("width:min(94vw,480px)"):
                ui.label(_("Distance thresholds — {name}").format(name=m["name"])).classes(
                    "text-base font-semibold text-gray-100")
                ui.label(_("Each model spreads distances differently. Empty = the value tuned for "
                           "multilingual-e5-large-instruct. The comparison below shows this model's "
                           "distances.")).classes("text-xs text-gray-500")
                f_arch = ui.number(_("Archive hint on web search: max. distance (e5: 0.15)"),
                                   value=th.get("archive_hint"), step=0.01, format="%.2f").props(
                    "outlined dark dense clearable").classes("w-full mt-2")
                f_dup = ui.number(_("\"Similar fact exists\": max. distance (e5: 0.15)"),
                                  value=th.get("fact_duplicate"), step=0.01, format="%.2f").props(
                    "outlined dark dense clearable").classes("w-full mt-2")
                f_hint = ui.number(_("Memory hint: min. similarity (empty = Settings > Documents)"),
                                   value=th.get("brain_hint"), step=0.05, format="%.2f").props(
                    "outlined dark dense clearable").classes("w-full mt-2")

                def _save() -> None:
                    reg.set_thresholds(m["id"], {"archive_hint": f_arch.value,
                                                 "fact_duplicate": f_dup.value,
                                                 "brain_hint": f_hint.value})
                    dlg.close()
                    model_list.refresh()

                with ui.row().classes("w-full justify-end gap-2 mt-4"):
                    ui.button(_("Cancel"), on_click=dlg.close).props("flat dark").classes("text-gray-400")
                    ui.button(_("Save"), on_click=_save).props("unelevated dark").classes("bg-primary text-white")
            dlg.open()

        # ── Comparison ───────────────────────────────────────────────────────

        ui.separator().classes("mt-2")
        with ui.row().classes("items-center gap-3 no-wrap"):
            ui.icon("compare_arrows", size="sm").classes("text-gray-400")
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(_("Compare indexes")).classes("text-base font-semibold text-gray-100")
                ui.label(_("Semantic search only, no language model: which documents each index finds, "
                           "and how fast. One model after the other, each alone in RAM.")).classes(
                    "text-xs text-gray-500")

        @ui.refreshable
        def compare_box() -> None:
            opts = {m["id"]: m["name"] for m in reg.models() if m.get("index") != "none"}
            if len(opts) < 2:
                ui.label(_("Two models with an index are needed. Add a model and build its index.")).classes(
                    "text-xs text-gray-500")
                return
            ids = list(opts)
            active = reg.active_id()
            a = active if active in opts else ids[0]
            b = next(x for x in ids if x != a)
            with ui.row().classes("w-full gap-2"):
                f_a = ui.select(opts, value=a, label="A").props("outlined dark dense").classes("flex-1 min-w-[200px]")
                f_b = ui.select(opts, value=b, label="B").props("outlined dark dense").classes("flex-1 min-w-[200px]")
            f_q = ui.textarea(_("Questions, one per line")).props("outlined dark dense autogrow").classes("w-full")
            with ui.row().classes("w-full items-center gap-2"):
                f_n = ui.number(_("Documents per question"), value=10, min=3, max=30, step=1,
                                format="%d").props("outlined dark dense").classes("w-48")
                btn = ui.button(_("Compare"), icon="play_arrow").props("unelevated dark").classes(
                    "bg-primary text-white")

            async def _run() -> None:
                qs = [q.strip() for q in (f_q.value or "").splitlines() if q.strip()]
                if not qs or f_a.value == f_b.value:
                    ui.notify(_("Enter questions and pick two different models."), type="warning")
                    return
                btn.props("loading")
                results.clear()
                with results:
                    ui.label(_("Running — loading a model can take a minute …")).classes("text-xs text-gray-500")
                try:
                    runs = await ei.compare([f_a.value, f_b.value], qs, int(f_n.value or 10))
                    await _render(runs, f_a.value, f_b.value, opts)
                except Exception as exc:  # noqa: BLE001
                    log.warning("comparison failed", exc_info=True)
                    results.clear()
                    with results:
                        ui.label(str(exc)).classes("text-xs px-2 py-1 adm-warn adm-mono")
                finally:
                    btn.props(remove="loading")
                    model_list.refresh()

            btn.on_click(_run)

        compare_box()
        results = ui.column().classes("w-full gap-2")

        async def _titles(ids: set[int]) -> dict[int, str]:
            from services.clients import get_session_paperless

            if not ids:
                return {}
            try:
                docs = await get_session_paperless().list_documents(ids=sorted(ids))
            except Exception:  # noqa: BLE001
                log.warning("titles for the comparison", exc_info=True)
                return {}
            return {d.id: d.title for d in docs}

        async def _render(runs: dict, a: str, b: str, names: dict) -> None:
            results.clear()
            if a not in runs or b not in runs:
                with results:
                    ui.label(_("One of the two indexes could not be searched.")).classes("text-xs adm-warn px-2 py-1")
                return
            ra, rb = runs[a], runs[b]
            all_ids = {d["id"] for r in (ra, rb) for q in r["questions"] for d in q["docs"]}
            titles = await _titles(all_ids)

            def med(vals):
                return statistics.median(vals) if vals else 0

            with results:
                # Summary
                with ui.element("div").classes("adm-cmp"):
                    for letter, mid, r in (("A", a, ra), ("B", b, rb)):
                        firsts = [q["docs"][0]["distance"] for q in r["questions"] if q["docs"]]
                        lasts = [q["docs"][-1]["distance"] for q in r["questions"] if q["docs"]]
                        with ui.column().classes("adm-card px-3 py-2 gap-0 min-w-0"):
                            ui.label(f"{letter} · {names.get(mid, mid)}").classes(
                                "w-full text-sm font-semibold text-gray-100 adm-ellipsis")
                            ui.label(_("Load {s} s · query embedding {e} ms · vector search {v} ms (median)").format(
                                s=r["load_s"], e=round(med([q["embed_ms"] for q in r["questions"]])),
                                v=round(med([q["search_ms"] for q in r["questions"]]), 1))).classes(
                                "text-xs text-gray-400")
                            ui.label(_("Distance of the best document {f}, of the last {l} (median) · {n} chunks").format(
                                f=round(med(firsts), 3), l=round(med(lasts), 3), n=r["chunks"])).classes(
                                "text-xs text-gray-500")
                ov = [ei.overlap(qa["docs"], qb["docs"]) for qa, qb in zip(ra["questions"], rb["questions"])]
                ui.label(_("Same documents in both lists, on average: {pct} %").format(
                    pct=round(100 * sum(ov) / max(1, len(ov))))).classes("text-sm text-gray-300")
                if any(not titles.get(i) for i in all_ids):
                    ui.label(_("Documents you cannot see in Paperless are shown by number only.")).classes(
                        "text-xs text-gray-500")

                for qa, qb in zip(ra["questions"], rb["questions"]):
                    with ui.column().classes("w-full adm-card px-3 py-2 gap-1"):
                        ui.label(qa["q"]).classes("w-full text-sm font-semibold text-gray-100")
                        rank_a = {d["id"]: i for i, d in enumerate(qa["docs"])}
                        rank_b = {d["id"]: i for i, d in enumerate(qb["docs"])}
                        with ui.element("div").classes("adm-cmp"):
                            for letter, q, other in (("A", qa, rank_b), ("B", qb, rank_a)):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(letter + " · " + _("{e} ms + {v} ms").format(
                                        e=q["embed_ms"], v=q["search_ms"])).classes("text-xs text-gray-500")
                                    for i, d in enumerate(q["docs"]):
                                        only = d["id"] not in other
                                        move = "" if only else other[d["id"]] - i
                                        title = titles.get(d["id"]) or _("Document #{id}").format(id=d["id"])
                                        with ui.element("div").classes("adm-doc" + (" only" if only else "")):
                                            ui.label(str(i + 1)).classes("adm-num")
                                            lab = ui.label(f"{title} · p. {d['page']}").classes("t")
                                            lab.tooltip(d.get("snippet", ""))
                                            note = _("only here") if only else (
                                                "" if move == 0 else (f"↑{move}" if move > 0 else f"↓{-move}"))
                                            ui.label(f"{d['distance']:.3f} {note}".strip()).classes("d")

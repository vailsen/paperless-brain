"""Settings > Documents: title documents from a template after AI analysis.

Three parts, each with its own guard:

- **The switch** renames documents the sync ingests from then on. Turning it on
  asks first: it overwrites titles, and it only renames *files* when Paperless
  is configured for it — which nothing here can check.
- **The template** is rendered live against an example, so a typo in a
  placeholder is visible before it reaches the archive.
- **Renaming existing documents** always starts with a dry run of up to
  `PREVIEW_SIZE` documents. The full run is a server-side task: it outlives the
  dialog and the page, and the dialog only watches it.
"""

from __future__ import annotations

from datetime import date

from nicegui import background_tasks, ui

from i18n import get_translator
from pipelines.title_rename import (
    DEFAULT_TEMPLATE,
    PLACEHOLDERS,
    PREVIEW_SIZE,
    RenamePlan,
    preview_renames,
    rename_documents,
    render_title,
)
from services.user_prefs import get_pref, set_pref

FILENAME_FORMAT_LINE = "PAPERLESS_FILENAME_FORMAT={{ title }}"

# One backfill per user, kept off the page so it survives closing the dialog.
_JOBS: dict[str, dict] = {}


def _template() -> str:
    return (get_pref("rename_title_template", DEFAULT_TEMPLATE) or "").strip() or DEFAULT_TEMPLATE


def _code(text: str) -> None:
    ui.label(text).classes("text-xs w-full").style(
        "font-family:ui-monospace,monospace; background:var(--c-bg);"
        "border:1px solid var(--c-border); border-radius:4px; padding:4px 8px;"
        "overflow-wrap:anywhere; color:var(--c-text)"
    )


def title_rename_setting(username: str, token: str) -> None:
    _ = get_translator()

    ui.separator().classes("my-4")

    async def _confirm_enable() -> bool:
        with ui.dialog() as dlg, ui.card().style(
            "background:var(--c-surface); width:min(94vw,520px);"
        ):
            ui.label(_("Before you turn this on")).classes("text-base font-semibold").style(
                "color:var(--c-text)"
            )
            ui.label(_(
                "After every sync, PaperlessBrain replaces the title of each newly "
                "analysed document with a name built from your template. The old "
                "title is kept in PaperlessBrain's record of renames, not in Paperless."
            )).classes("text-sm w-full").style("color:var(--c-text-2)")
            ui.label(_(
                "Paperless-ngx renames the file itself only if its filename format "
                "uses the title. Add this line to docker-compose.env in your "
                "Paperless-ngx folder, then restart Paperless:"
            )).classes("text-sm w-full").style("color:var(--c-text-2)")
            _code(FILENAME_FORMAT_LINE)
            ui.label(_(
                "Without it, only the title changes — the files keep their names."
            )).classes("text-sm w-full").style("color:var(--c-warn)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(False)).props("flat dense")
                ui.button(_("Turn on"), on_click=lambda: dlg.submit(True)).props(
                    "unelevated dense color=purple"
                )
        answer = await dlg
        dlg.delete()
        return bool(answer)

    async def _on_toggle(e) -> None:
        if e.value and not get_pref("rename_titles", False):
            if not await _confirm_enable():
                switch.set_value(False)
                return
        set_pref("rename_titles", bool(e.value))

    switch = ui.checkbox(
        _("Rename documents after AI analysis"),
        value=bool(get_pref("rename_titles", False)),
        on_change=_on_toggle,
    ).classes("text-sm text-gray-300")

    # ── Template + live preview ──────────────────────────────────────────────
    template_input = ui.input(_("Name template"), value=_template()).props(
        "outlined dense dark"
    ).classes("w-full mt-2")
    ui.label(
        _("Placeholders: {placeholders}").format(
            placeholders=" ".join("{%s}" % p for p in PLACEHOLDERS)
        )
    ).classes("text-xs w-full").style("color:var(--c-text-muted); overflow-wrap:anywhere")

    with ui.row().classes("w-full items-center gap-2 no-wrap mt-1"):
        ui.label(_("Example:")).classes("text-xs shrink-0").style("color:var(--c-text-muted)")
        preview = ui.label("").classes("text-xs flex-1 w-full").style(
            "font-family:ui-monospace,monospace; color:var(--c-text); overflow-wrap:anywhere"
        )

    def _render_preview() -> None:
        title = render_title(
            template_input.value or DEFAULT_TEMPLATE,
            created=date(2026, 10, 17),
            doc_type=_("Invoice"),
            description=_("Graphics card"),
            correspondent="Amazon",
        )
        preview.set_text(f"{title}.pdf" if title else _("(empty — the template produces no text)"))

    def _on_template(e) -> None:
        set_pref("rename_title_template", (e.value or "").strip())
        _render_preview()

    template_input.on_value_change(_on_template)
    _render_preview()

    with ui.row().classes("w-full gap-2 mt-1"):
        ui.button(
            _("Reset to default"), on_click=lambda: template_input.set_value(DEFAULT_TEMPLATE)
        ).props("flat dense size=sm")
        ui.button(
            _("Rename existing documents…"), icon="drive_file_rename_outline",
            on_click=lambda: _open_backfill(username, token),
        ).props("flat dense size=sm")

    with ui.element("div").classes(
        "bg-gray-900 border border-gray-700 rounded p-3 text-xs text-gray-400 mt-2 mb-3 w-full"
    ):
        ui.label(_(
            "PaperlessBrain sets the document title; Paperless-ngx builds the file "
            "name from it. For the file on disk to carry this name, add the line "
            "below to docker-compose.env in your Paperless-ngx folder (not to .env) "
            "and restart Paperless. Files that already exist are renamed when their "
            "title changes, or all at once with Paperless' document_renamer command."
        )).classes("w-full")
        _code(FILENAME_FORMAT_LINE)
        ui.label(_(
            "Paperless' own download button always prefixes the date and the "
            "correspondent; the name above is the one on disk."
        )).classes("w-full mt-1")


def _open_backfill(username: str, token: str) -> None:
    """Dry run first, then — on request — rename every pending document."""
    _ = get_translator()
    from services.clients import get_session_paperless, sidecar_service
    from services.vision import build_vision_client

    paperless = get_session_paperless()
    vision = build_vision_client(username, token)
    template = _template()
    # The background tasks only change `view` and the status text; the timer
    # re-renders, so nothing builds elements outside the dialog's context, and
    # the preview list is drawn once instead of every tick (which would reset
    # its scroll position).
    view: dict = {"phase": "previewing", "plans": [], "total": 0, "error": "", "drawn": None,
                  "skipped": []}
    if (job := _JOBS.get(username)) and job.get("running"):
        view["phase"] = "running"

    with ui.dialog() as dlg, ui.card().style(
        "background:var(--c-surface); width:min(96vw,760px); max-width:96vw;"
    ):
        ui.label(_("Rename existing documents")).classes("text-base font-semibold").style(
            "color:var(--c-text)"
        )
        status = ui.label("").classes("text-sm w-full").style("color:var(--c-text-2)")

        @ui.refreshable
        def _body() -> None:
            phase = view["phase"]
            if phase == "previewing":
                ui.spinner(size="lg").classes("self-center my-4")
                return
            if phase == "error":
                ui.label(view["error"]).classes("text-sm w-full").style("color:var(--c-warn)")
            if phase == "preview" and not view["total"]:
                ui.button(_("Close"), on_click=dlg.close).props("flat dense").classes("self-end")
                return
            if phase == "preview":
                _render_plans(view["plans"])
                if view["skipped"]:
                    with ui.expansion(
                        # TODO i18n-plural
                        _("{n} documents could not be named").format(n=len(view["skipped"])),
                    ).classes("w-full text-xs"):
                        ui.label("\n".join(view["skipped"])).classes("text-xs w-full").style(
                            "font-family:ui-monospace,monospace; white-space:pre-wrap;"
                            "overflow-wrap:anywhere; color:var(--c-text-muted)"
                        )
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button(_("Cancel"), on_click=dlg.close).props("flat dense")
                    if view["total"]:
                        ui.button(
                            # TODO i18n-plural
                            _("Rename all {n} documents").format(n=view["total"]),
                            on_click=_start,
                        ).props("unelevated dense color=purple")
                return
            if phase == "running":
                job = _JOBS.get(username) or {}
                log_lines = job.get("log", [])[-8:]
                if log_lines:
                    ui.label("\n".join(log_lines)).classes("text-xs w-full").style(
                        "font-family:ui-monospace,monospace; white-space:pre-wrap;"
                        "overflow-wrap:anywhere; color:var(--c-text-muted)"
                    )
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    if job.get("running"):
                        ui.button(_("Stop"), on_click=lambda: job.update(stop=True)).props(
                            "flat dense"
                        )
                    ui.button(_("Close"), on_click=dlg.close).props("flat dense")
                return
            ui.button(_("Close"), on_click=dlg.close).props("flat dense")

        _body()
        view["drawn"] = view["phase"]

    def _render_plans(plans: list[RenamePlan]) -> None:
        if not plans:
            ui.label(_("None of the examples could be named.")).classes(
                "text-sm w-full"
            ).style("color:var(--c-text-muted)")
            return
        with ui.element("div").classes("w-full").style(
            "max-height:50vh; overflow-y:auto; border:1px solid var(--c-border);"
            "border-radius:6px; padding:4px 8px;"
        ):
            for plan in plans:
                with ui.column().classes("w-full gap-0 py-1").style(
                    "border-bottom:1px solid var(--c-border)"
                ):
                    ui.label(f"#{plan.doc_id}  {plan.old_title}").classes("text-xs w-full").style(
                        "color:var(--c-text-muted); overflow-wrap:anywhere"
                    )
                    ui.label(f"→ {plan.new_title}").classes("text-sm w-full").style(
                        "color:var(--c-text); font-family:ui-monospace,monospace;"
                        "overflow-wrap:anywhere"
                    )

    def _start() -> None:
        job = {"running": True, "stop": False, "renamed": 0, "failed": 0,
               "total": view["total"], "log": [], "error": ""}
        _JOBS[username] = job

        async def _run() -> None:
            try:
                renamed, failed = await rename_documents(
                    paperless, sidecar_service, vision.describe_for_title, template,
                    log=job["log"].append,
                    progress=lambda done, total: job.update(renamed=done, total=total),
                    should_stop=lambda: job["stop"],
                )
                job.update(renamed=renamed, failed=failed)
            except Exception as exc:  # noqa: BLE001 — shown in the dialog
                job["error"] = str(exc)
            finally:
                job["running"] = False

        background_tasks.create(_run(), name=f"title-backfill-{username}")
        view["phase"] = "running"
        _tick()

    def _tick() -> None:
        if view["phase"] != "running":
            if view["drawn"] != view["phase"]:
                view["drawn"] = view["phase"]
                _body.refresh()
            return
        job = _JOBS.get(username) or {}
        if job.get("running"):
            status.set_text(
                _("Renamed {done} of {total} …").format(
                    done=job.get("renamed", 0), total=job.get("total", 0)
                )
            )
        elif job.get("error"):
            status.set_text(_("Stopped with an error: {err}").format(err=job["error"]))
        else:
            status.set_text(
                _("Finished: {renamed} renamed, {failed} failed.").format(
                    renamed=job.get("renamed", 0), failed=job.get("failed", 0)
                )
            )
        _body.refresh()

    with dlg:
        ui.timer(0.5, _tick)
    dlg.on("hide", dlg.delete)
    dlg.open()

    if view["phase"] != "previewing":
        _tick()
        return

    status.set_text(
        _("Naming up to {n} documents with the ingestion model — this can take a few minutes …").format(
            n=PREVIEW_SIZE
        )
    )

    def _say(text: str) -> None:
        try:
            status.set_text(text)
        except Exception:  # noqa: BLE001 — the dialog was closed meanwhile
            pass

    async def _preview() -> None:
        def _progress(done: int, total: int) -> None:
            _say(_("Named {done} of {total} …").format(done=done, total=total))

        try:
            plans, total = await preview_renames(
                paperless, sidecar_service, vision.describe_for_title, template,
                progress=_progress, log=lambda line: view["skipped"].append(line.strip()),
            )
        except Exception as exc:  # noqa: BLE001 — shown in the dialog
            view.update(phase="error", error=str(exc))
            _say("")
            return
        view.update(phase="preview", plans=plans, total=total)
        if total:
            # TODO i18n-plural
            _say(_(
                "{total} documents have not been renamed yet. These are the first "
                "{n} as they would be named — nothing has been changed in Paperless."
            ).format(total=total, n=len(plans)))
        else:
            _say(_("Every analysed document has already been renamed."))

    background_tasks.create(_preview(), name=f"title-preview-{username}")

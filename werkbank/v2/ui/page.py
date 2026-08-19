"""Werkbank v2 — the /werkbank page. This is the live deep-research module.

Flow: request → brief → **user confirms** → run in the background → board and
report. The confirmation step is not decoration: a reformulation narrows a task
silently, and this is the cheapest possible place to catch that, before any
model time is spent.

Runs execute as background tasks. The page polls the store rather than holding
the run in memory, so closing the tab does not abort anything and reopening
shows the state as it is.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from i18n import get_translator
from services.session_auth import get_session_token
from werkbank.v2 import pipeline, store
from werkbank.v2.models import SubtaskStatus
from werkbank.v2.ui import board as board_ui
from werkbank.v2.ui.brief_dialog import confirm_brief
from werkbank.v2.ui.style import BOARD_CSS

_log = logging.getLogger(__name__)

# Background runs, so a closed tab does not cancel one.
_running: set[asyncio.Task] = set()

# Runs whose dialog is open right now. Polling pauses while one is: the numbers
# behind an open report do not change often enough to be worth re-rendering the
# list underneath it.
_open_dialogs: set[str] = set()

STATUS_WORDS = {
    # Short by necessity: these sit on one line beside the title on a phone.
    "briefing": "briefing",
    "briefing_failed": "briefing failed",
    "draft": "needs review",
    "planned": "planned",
    "running": "running",
    "done": "done",
    "failed": "failed",
}

# Colour encodes state, not identity: accent while something is working, warn
# only when the user has to act. "done" and "planned" stay neutral — a board
# where everything is coloured shows nothing.
TONE_ACTIVE = "is-running"
TONE_ATTENTION = "is-attention"


def run_tone(status: str) -> str:
    return {
        "running": TONE_ACTIVE,
        "briefing": TONE_ACTIVE,
        # The user has to act on these two, and on nothing else.
        "draft": TONE_ATTENTION,
        "briefing_failed": TONE_ATTENTION,
        "failed": TONE_ATTENTION,
    }.get(status, "")


def local_time(iso: str) -> str:
    """Stored timestamps are UTC; the user reads them in their own timezone.

    Slicing the ISO string was two hours off in Germany all summer — the store
    is right, the display was lying about when a run happened.
    """
    from datetime import datetime

    from config.settings import local_tz

    try:
        stamp = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return (iso or "")[:16].replace("T", " ")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(local_tz()).strftime("%Y-%m-%d %H:%M")


def _models(user_id: str, token: str) -> list[str]:
    try:
        from services.model_registry import model_names

        if names := model_names(user_id, token):
            return names
    except Exception:
        pass
    return ["claude-sonnet-4-6"]


def start_briefing(user_id: str, token: str, model: str, request: str, refresh) -> str:
    """Put the run in the list, then formulate its assignment in the background.

    The dialog used to stay open through the briefer call and the request lived
    only inside it: leaving the page threw the work away, so the user had to sit
    and watch a screen that was doing nothing for them. Now the row is there
    immediately and they can come back to a brief that is waiting.
    """
    _ = get_translator()
    pipeline.register_token(user_id, token)
    run_id = pipeline.create_draft(request, user_id, model)
    refresh()

    async def go() -> None:
        try:
            await pipeline.brief_draft(run_id, user_id, model)
        except Exception as exc:                            # noqa: BLE001
            _log.exception("werkbank v2: briefing %s failed", run_id)
            ui.notify(
                _("Could not create the brief: {err}").format(err=exc), type="negative"
            )
        finally:
            refresh()

    task = asyncio.create_task(go())
    _running.add(task)
    task.add_done_callback(_running.discard)
    pipeline.track(run_id, task)
    return run_id


async def review_draft(run_id: str, user_id: str, model: str, refresh, host=None) -> None:
    """Show the finished brief for confirmation, and start the run on approval."""
    _ = get_translator()
    state = store.load_state(run_id, user_id)
    if state is None or state.brief is None:
        ui.notify(_("Run not found."), type="warning")
        return

    _open_dialogs.add(run_id)
    try:
        confirmed = await confirm_brief(state.brief, host=host)
    finally:
        _open_dialogs.discard(run_id)
    if confirmed is None:                                   # cancelled: it waits
        return

    pipeline.confirm_draft(run_id, user_id, confirmed)
    store.set_run_status(run_id, user_id, "running")
    refresh()
    _launch(run_id, user_id, model, refresh)
    ui.notify(_("Run started."))


def _launch(run_id: str, user_id: str, model: str, refresh) -> None:
    async def go() -> None:
        try:
            await pipeline.start_run(run_id, user_id, model)
        except Exception as exc:                       # never lose the run silently
            _log.exception("werkbank v2 run %s failed", run_id)
            store.set_run_status(run_id, user_id, "failed")
            ui.notify(f"Run {run_id}: {exc}", type="negative")
        finally:
            refresh()

    task = asyncio.create_task(go())
    _running.add(task)
    task.add_done_callback(_running.discard)
    pipeline.track(run_id, task)


def _report_filename(state, run_id: str, suffix: str) -> str:
    goal = (state.brief.goal if state.brief else "") or "werkbank"
    slug = re.sub(r"[^\w\s-]", "", goal)[:40].strip().replace(" ", "_") or run_id
    return f"{datetime.now().strftime('%Y%m%d')}_{slug}.{suffix}"


async def _download_pdf(state, report: str, run_id: str, user_id: str) -> None:
    """The report as a PDF, straight to the browser.

    Same renderer as the Paperless upload, so the two produce the same document
    — this one just does not put it in the archive. Rendering blocks, so it runs
    in a thread; on a long report that is a second or two.
    """
    _ = get_translator()
    notice = ui.notification(_("Preparing the PDF…"), spinner=True, timeout=None)
    try:
        from config.settings import local_tz
        from services.pdf_generator import generate_chat_pdf

        title = (state.brief.goal if state.brief else _("Research report"))[:120]
        pdf = await asyncio.to_thread(
            generate_chat_pdf, report, title, user_id, state.model,
            datetime.now(tz=local_tz()),
        )
        notice.dismiss()
        ui.download(pdf, _report_filename(state, run_id, "pdf"), media_type="application/pdf")
    except Exception as exc:                                # noqa: BLE001
        notice.dismiss()
        _log.exception("werkbank v2: PDF export failed")
        ui.notify(_("Could not create the PDF: {err}").format(err=exc), type="negative")


async def _file_report(state, report: str, user_id: str) -> None:
    """File the finished report into Paperless as a PDF.

    Review-before-persist, unchanged from v1: this runs on a button, never at
    the end of a run. A research result is the model's reading of the archive
    and must not enter the archive without the user saying so.
    """
    _ = get_translator()
    title = (state.brief.goal if state.brief else _("Research report"))[:120]
    notice = ui.notification(_("Uploading…"), spinner=True, timeout=None)
    try:
        from services.clients import get_session_paperless
        from werkbank.export import upload_pdf

        _task_id, filename = await upload_pdf(
            content_markdown=report,
            title=title,
            username=user_id,
            model_name=state.model,
            filename_slug=title,
            paperless_client=get_session_paperless(),
        )
        notice.dismiss()
        ui.notify(_("Saved as {name}").format(name=filename), type="positive")
    except Exception as exc:                                # noqa: BLE001
        notice.dismiss()
        ui.notify(_("Upload failed: {err}").format(err=exc), type="negative")


def _delete_run(run_id: str, user_id: str, refresh, host=None) -> None:
    """Ask, then stop it, then delete it.

    Asking, because a finished run is twenty minutes of model time and a report
    that cannot be reproduced — and the button sits next to the one that opens
    it. Stopping first, because a running task writes its next result after the
    row is gone and the run reappears, which reads as the delete not working.
    """
    _ = get_translator()
    state = store.load_state(run_id, user_id)
    goal = (state.brief.goal or state.brief.original_request) if state and state.brief else run_id
    running = store.list_runs(user_id)
    status = next((r["status"] for r in running if r["run_id"] == run_id), "")

    def _do() -> None:
        dialog.close()
        pipeline.cancel_run(run_id)
        store.delete_run(run_id, user_id)
        _open_dialogs.discard(run_id)
        refresh()
        ui.notify(_("Run deleted."))

    container = host if host is not None else ui.context.slot.parent
    with container, ui.dialog() as dialog, ui.card().style(
        "background:var(--c-surface); width:min(94vw,460px)"
    ):
        ui.label(_("Delete this run?")).classes("text-base font-semibold").style(
            "color:var(--c-text)")
        ui.label(goal).classes("text-sm mt-1").style(
            "color:var(--c-text-2); overflow-wrap:anywhere")
        if status == "running":
            ui.label(_("It is still running — it will be stopped.")).classes(
                "text-xs mt-2").style("color:var(--c-warn)")
        ui.label(
            _("The report and every fact behind it are gone for good.")
        ).classes("text-xs mt-2").style("color:var(--c-text-muted)")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            ui.button(_("Keep"), color=None, on_click=dialog.close).props(
                "flat dense"
            ).style("color:var(--c-text-2)")
            ui.button(_("Delete"), icon="delete_outline", color=None,
                      on_click=_do).props("flat dense").style("color:var(--c-warn)")

    _open_dialogs.add(run_id)
    dialog.on("hide", lambda _e=None: _open_dialogs.discard(run_id))
    dialog.open()


def _open_run(run_id: str, user_id: str, model: str, refresh, host) -> None:
    """A run's board and, once it exists, its report.

    Built inside `host`, a page-level element *outside* the refreshable run
    list. A dialog created inside the refreshable slot is a child of it, so the
    poll timer's next refresh deletes the open dialog — which looked like the
    dialog closing itself after a couple of seconds.

    The body refreshes on its own timer while the dialog is open: this is the
    one view where a running subtask is being watched, so freezing it would be
    exactly backwards.
    """
    _ = get_translator()
    state = store.load_state(run_id, user_id)
    if state is None:
        ui.notify(_("Run not found."), type="warning")
        return

    with host, ui.dialog().props("maximized") as dialog, ui.card().style(
        "background:var(--c-bg); width:100%; height:100%; overflow-y:auto"
    ):

        def _close() -> None:
            dialog.close()
            _open_dialogs.discard(run_id)
            refresh()

        # Survives the 3-second refresh: rebuilding the body would otherwise
        # collapse a section the user just opened, every three seconds.
        expanded = {"assignment": False}

        @ui.refreshable
        def body() -> None:
            run = store.load_state(run_id, user_id)
            if run is None:
                return
            report = store.load_report(run_id, user_id)

            # The title takes its own line on a phone: two labelled actions
            # beside it squeeze it to an ellipsis, and then the dialog says
            # nothing about which run is open.
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.label(run.brief.goal if run.brief else run_id).classes(
                    "text-lg font-semibold basis-full sm:basis-0 sm:flex-1 min-w-0"
                ).style("color:var(--c-text); overflow:hidden; text-overflow:ellipsis;"
                        "white-space:nowrap")
                if report:
                    ui.button(
                        _("Markdown"), icon="download", color=None,
                        on_click=lambda r=run, t=report: ui.download(
                            t.encode("utf-8"), _report_filename(r, run_id, "md"),
                            media_type="text/markdown"),
                    ).props("flat dense").style("color:var(--c-text-2)")
                    ui.button(
                        _("PDF"), icon="picture_as_pdf", color=None,
                        on_click=lambda r=run, t=report: _download_pdf(
                            r, t, run_id, user_id),
                    ).props("flat dense").style("color:var(--c-text-2)")
                    ui.button(
                        _("Save to Paperless"), icon="save", color=None,
                        on_click=lambda r=run, t=report: _file_report(r, t, user_id),
                    ).props("flat dense").style("color:var(--c-text-2)")
                ui.button(icon="close", color=None, on_click=_close).props(
                    "flat dense round"
                ).classes("card-action-btn")

            if run.brief:
                with ui.expansion(
                    _("Assignment"), value=expanded["assignment"]
                ).classes("w-full").on_value_change(
                    lambda e: expanded.__setitem__("assignment", e.value)
                ):
                    ui.label(run.brief.original_request).classes("text-sm").style(
                        "color:var(--c-text-2); white-space:pre-wrap")
                    for criterion in run.brief.acceptance_criteria:
                        ui.label(f"• {criterion}").classes("text-xs").style(
                            "color:var(--c-text-muted)")

            board_ui.board(run)
            if report:
                ui.separator().classes("my-3")
                board_ui.render_report(report, run)
            else:
                ui.label(_("The report appears when the run has finished.")).classes(
                    "text-sm mt-4"
                ).style("color:var(--c-text-muted)")

        body()

        def _tick() -> None:
            # Stop polling once there is nothing left to change: an open report
            # would otherwise re-render every three seconds forever.
            if store.load_report(run_id, user_id) and not _has_open_work(run_id, user_id):
                return
            # A fact dialog opened from the board lives in the board's slot, so
            # refreshing would close it under the user's hand.
            if board_ui.dialogs_open():
                return
            body.refresh()

        ui.timer(3.0, _tick)

    _open_dialogs.add(run_id)
    dialog.on("hide", lambda _e=None: _open_dialogs.discard(run_id))
    dialog.open()


def _has_open_work(run_id: str, user_id: str) -> bool:
    return bool(store.pending_subtask_ids(run_id, user_id))


@ui.page("/werkbank")
def werkbank_page() -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html(BOARD_CSS)

    user_id: str = ng_app.storage.user.get("paperless_user", "")
    token: str = get_session_token()
    pipeline.register_token(user_id, token)

    from werkbank.archetypes import seed_defaults_if_needed

    seed_defaults_if_needed(user_id)
    models = _models(user_id, token)

    @ui.refreshable
    def run_list() -> None:
        runs = store.list_runs(user_id)
        if not runs:
            with ui.column().classes("w-full items-center gap-2 mt-10"):
                ui.icon("auto_awesome", size="32px").style("color:var(--c-text-muted)")
                ui.label(_("No runs yet.")).classes("text-sm").style(
                    "color:var(--c-text-muted)")
            return

        for run in runs:
            state = store.load_state(run["run_id"], user_id)
            status = run["status"]
            brief = state.brief if state else None
            # While the briefer is still working there is no goal yet — the
            # user's own words are what identifies the row until there is.
            goal = (
                (brief.goal if brief and brief.goal else "")
                or (brief.original_request if brief else "")
                or run["run_id"]
            )
            done = sum(
                1 for r in (state.results.values() if state else [])
                if r.status in (SubtaskStatus.OK, SubtaskStatus.PARTIAL,
                                SubtaskStatus.UNRESOLVABLE)
            )
            total = len(state.subtasks) if state and state.subtasks else 0
            tone = run_tone(status)

            with ui.element("div").classes(f"wb-card w-full {tone}"):
                # Title on its own line, everything else below it. A status word
                # beside the title squeezed the title to nothing on a phone.
                ui.label(goal).classes("wb-question w-full").style(
                    "overflow:hidden; text-overflow:ellipsis; white-space:nowrap")
                with ui.row().classes("w-full items-center gap-2 mt-1 no-wrap"):
                    # Two groups: the facts may wrap, the buttons never do —
                    # a delete button on a line of its own reads as a mistake.
                    with ui.row().classes("items-center gap-2 flex-wrap flex-1 min-w-0"):
                        ui.html(
                            f'<span class="wb-pill {tone}">'
                            f"{_(STATUS_WORDS.get(status, status))}</span>",
                            sanitize=False,
                        )
                        ui.label(local_time(run["created_at"])).classes("wb-meta")
                        if total:
                            ui.label(
                                _("{done}/{total} subtasks").format(done=done, total=total)
                            ).classes("wb-meta")
                        ui.label(run["model"]).classes("wb-agent wb-hide-narrow").style(
                            "color:var(--c-text-muted)")
                    with ui.row().classes("items-center gap-1 flex-shrink-0 no-wrap"):
                        if status == "draft":
                            ui.button(
                                _("Review"), icon="fact_check", color=None,
                                on_click=lambda rid=run["run_id"],
                                m=run["model"] or models[0]: review_draft(
                                    rid, user_id, m, run_list.refresh, dialog_host),
                            ).props("flat dense").style("color:var(--c-accent)")
                        if status == "briefing":
                            ui.spinner(size="sm").props("color=purple")
                        if status in ("planned", "failed"):
                            ui.button(
                                icon="play_arrow", color=None,
                                on_click=lambda rid=run["run_id"],
                                m=run["model"] or models[0]: (
                                    store.set_run_status(rid, user_id, "running"),
                                    run_list.refresh(),
                                    _launch(rid, user_id, m, run_list.refresh),
                                ),
                            ).props("flat dense round").classes(
                                "card-action-btn"
                            ).tooltip(_("Start / resume"))
                        if status not in ("briefing", "draft"):
                            ui.button(
                                icon="open_in_new", color=None,
                                on_click=lambda rid=run["run_id"], m=run["model"]:
                                    _open_run(rid, user_id, m or models[0],
                                              run_list.refresh, dialog_host),
                            ).props("flat dense round").classes("card-action-btn")
                        ui.button(
                            icon="delete_outline", color=None,
                            on_click=lambda rid=run["run_id"]: _delete_run(
                                rid, user_id, run_list.refresh, dialog_host),
                        ).props("flat dense round").classes("card-action-btn")

    def _open_new_dialog() -> None:
        with ui.dialog() as dialog, ui.card().style(
            "background:var(--c-surface); width:min(94vw,640px)"
        ):
            ui.label(_("New research assignment")).classes("text-base font-semibold").style(
                "color:var(--c-text)")
            request = ui.textarea(
                label=_("What should be researched?"),
                placeholder=_("e.g. Which notice periods apply to my contracts, "
                              "and which of them are statutory?"),
            ).props("outlined dense autogrow").classes("w-full")
            model = ui.select(models, value=models[0], label=_("Model")).props(
                "outlined dense").classes("w-full mt-2")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), color=None, on_click=dialog.close).props(
                    "flat dense"
                ).style("color:var(--c-text-2)")

                def _go() -> None:
                    text = (request.value or "").strip()
                    if not text:
                        return
                    dialog.close()
                    start_briefing(user_id, token, model.value, text, run_list.refresh)
                    ui.notify(
                        _("Formulating the assignment — you can leave this page.")
                    )

                ui.button(_("Continue"), on_click=_go).props("unelevated dense color=purple")
        dialog.open()

    with ui.column().classes("w-full max-w-5xl mx-auto px-4 py-6 gap-3").style("min-width:0"):
        with ui.row().classes(
            "w-full items-center justify-between gap-2 flex-wrap"
        ):
            with ui.row().classes("items-center gap-2 min-w-0"):
                ui.icon("auto_awesome", size="md").style("color:var(--c-text-muted)")
                ui.label(_("AI deep research")).classes("text-xl sm:text-2xl font-bold").style(
                    "color:var(--c-text)")
            with ui.row().classes("gap-2 flex-shrink-0 items-center"):
                def _open_archetypes() -> None:
                    from werkbank.ui.archetype_dialog import open_archetype_dialog

                    open_archetype_dialog(user_id)

                ui.button(
                    _("Agents"), icon="groups", color=None, on_click=_open_archetypes
                ).props("flat dense").style("color:var(--c-text-2)")
                ui.button(_("New run"), icon="add", on_click=_open_new_dialog).props(
                    "unelevated dense color=purple")

        run_list()

    # Dialogs are built in here: a page-level element the poll timer never
    # rebuilds. Inside the refreshable slot they are destroyed on the next tick.
    dialog_host = ui.element("div")

    # Polling rather than pushing: a run outlives the page that started it.
    def _poll() -> None:
        if not _open_dialogs:
            run_list.refresh()

    ui.timer(4.0, _poll)

"""The skill wizard dialog (docs/experts.md §7.2, `services/skill_wizard.py`).

Model → goal → interview → draft (+ lint) → try it side by side → save.
One step per screen; on a phone the dialog is full screen. The model drafts,
the user saves.
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from i18n import get_translator

log = logging.getLogger("skill_wizard")


def open_wizard(*, user: str, token: str, models: list[dict], default_model: str, on_saved=None) -> None:
    from app_ui.model_picker import model_select
    from services import experts, skill_wizard, skills
    from services.user_prefs import get_pref, set_pref

    _ = get_translator()
    lang = get_pref("language", "en")
    st: dict = {"step": 0, "questions": [], "answers": [], "draft": None, "busy": False}
    model0 = get_pref("skill_wizard_model", "") or default_model

    with ui.dialog() as dlg, ui.card().style(
        "background:var(--c-surface); width:min(760px,98vw); max-width:98vw; max-height:94vh; overflow-y:auto;"
        "border:1px solid var(--c-border); gap:10px;"
    ):
        with ui.row().classes("w-full items-center no-wrap"):
            ui.icon("auto_awesome", size="sm").classes("text-gray-400")
            ui.label(_("Skill wizard")).classes("text-base font-semibold flex-1").style("color:var(--c-text)")
            step_lbl = ui.label("").classes("text-xs").style("color:var(--c-text-muted)")
            ui.button(icon="close", on_click=dlg.close).props("flat dense round").classes("card-action-btn")
        body = ui.column().classes("w-full gap-3")
        status = ui.label("").classes("text-xs").style("color:var(--c-text-muted)")
        with ui.row().classes("w-full justify-between gap-2"):
            back_btn = ui.button(_("Back"), icon="arrow_back").props("flat no-caps")
            next_btn = ui.button(_("Next"), icon="arrow_forward").props("unelevated no-caps").classes("bg-primary text-white")

    fields: dict = {}
    steps = [_("Model"), _("Goal"), _("Questions"), _("Draft"), _("Try it")]

    def render() -> None:
        body.clear()
        step_lbl.set_text(_("Step {n} of {total}: {name}").format(n=st["step"] + 1, total=len(steps), name=steps[st["step"]]))
        back_btn.set_visibility(st["step"] > 0)
        next_btn.set_text(_("Save skill") if st["step"] == 4 else _("Next"))
        with body:
            [_step_model, _step_goal, _step_questions, _step_draft, _step_try][st["step"]]()

    def _step_model() -> None:
        ui.label(_("Which model should help you write the skill? A strong model writes better instructions; "
                   "the skill then works with any model.")).classes("text-sm").style("color:var(--c-text-2)")
        fields["model"] = model_select(models, model0 if any(m["name"] == model0 for m in models) else default_model,
                                       label=_("Model")).props("outlined dense").classes("w-full")

    def _step_goal() -> None:
        ui.label(_("What should an expert be able to do with this skill?")).classes("text-sm").style("color:var(--c-text-2)")
        fields["goal"] = ui.textarea(value=fields.get("goal_v", ""), placeholder=_(
            "e.g. Check my electricity bills: compare the price per kWh with the contract, flag back payments.")).props(
            "outlined autogrow").classes("w-full")
        ui.label(_("Optional: an example question — and what a good answer would contain.")).classes("text-xs").style(
            "color:var(--c-text-muted)")
        fields["example"] = ui.textarea(value=fields.get("example_v", ""), placeholder=_(
            "Question: Is my last electricity bill correct?\nGood answer: price per kWh vs. contract, base fee, "
            "difference in euros, the pages it comes from.")).props("outlined autogrow").classes("w-full")

    def _step_questions() -> None:
        if not st["questions"]:
            ui.label(_("No questions — continue to the draft.")).classes("text-sm").style("color:var(--c-text-2)")
            return
        ui.label(_("A few questions to sharpen the skill. Skip any you like.")).classes("text-sm").style(
            "color:var(--c-text-2)")
        st["answers"] = []
        for q in st["questions"]:
            ui.label(q["question"]).classes("text-sm font-semibold").style("color:var(--c-text)")
            inp = ui.input(value=q.get("answer", "")).props("outlined dense").classes("w-full")
            if q.get("suggestions"):
                with ui.row().classes("gap-1 flex-wrap"):
                    for sug in q["suggestions"]:
                        ui.button(sug, on_click=lambda _e, i=inp, s=sug: i.set_value(s)).props(
                            "outline dense no-caps size=sm").classes("text-xs")
            st["answers"].append((q, inp))

    def _step_draft() -> None:
        d = st["draft"] or {"name": "", "description": "", "body": ""}
        fields["name"] = ui.input(_("Name"), value=d["name"]).props("outlined dense").classes("w-full")
        fields["desc"] = ui.input(_("When to use it"), value=d["description"]).props("outlined dense").classes("w-full")
        fields["body"] = ui.textarea(_("Instructions"), value=d["body"]).props("outlined autogrow").classes(
            "w-full").style("min-height:200px;")
        lint_box = ui.column().classes("w-full gap-1")

        def _lint() -> None:
            lint_box.clear()
            texts = {
                "no_name": _("The skill has no name."),
                "no_description": _("Say when to use it — that is how the model decides."),
                "description_trigger": _("Tip: start the description with \"Use when …\"."),
                "too_long": _("Too long — only the first {n} characters are used."),
                "unknown_tool": _("No such tool: {t}. The model would try to call it and fail."),
                "tool_off": _("This tool is switched off for an expert using the skill: {t}"),
            }
            with lint_box:
                for f in skill_wizard.lint(fields["name"].value, fields["desc"].value, fields["body"].value):
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        ui.icon("warning" if f["level"] == "warn" else "info", size="xs").style(
                            "color:var(--c-warn)" if f["level"] == "warn" else "color:var(--c-text-muted)")
                        ui.label(texts.get(f["code"], f["code"]).format(n=f["text"], t=f["text"])).classes(
                            "text-xs").style("color:var(--c-text-2); overflow-wrap:anywhere;")

        _lint()
        fields["body"].on_value_change(lambda _e: _lint())
        fields["desc"].on_value_change(lambda _e: _lint())

    def _step_try() -> None:
        ui.label(_("Ask the example question twice — without and with the skill — and compare. "
                   "Read-only tools only: a trial changes nothing.")).classes("text-sm").style("color:var(--c-text-2)")
        exs = experts.list_experts(user)
        with ui.row().classes("w-full gap-2 items-center"):
            fields["try_expert"] = ui.select({e.expert_id: e.name for e in exs}, value=experts.UNIVERSAL_ID,
                                             label=_("Expert")).props("outlined dense").classes("flex-1").style(
                "min-width:10rem;")
            run_btn = ui.button(_("Run both"), icon="play_arrow").props("unelevated no-caps dense").classes(
                "bg-primary text-white")
        fields["try_q"] = ui.input(_("Question"), value=_first_question(fields.get("example_v", ""))).props(
            "outlined dense").classes("w-full")
        with ui.element("div").classes("w-full").style(
            "display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px;"):
            cols = []
            for title in (_("Without the skill"), _("With the skill")):
                with ui.column().classes("gap-1").style(
                    "border:1px solid var(--c-border); border-radius:8px; padding:8px 10px; min-width:0;"):
                    ui.label(title).classes("text-xs font-semibold").style("color:var(--c-text-2)")
                    cols.append(ui.markdown("").classes("chat-md text-sm w-full").style("overflow-wrap:anywhere;"))

        async def _run() -> None:
            if st["busy"]:
                return
            q = (fields["try_q"].value or "").strip()
            rm = next((m for m in models if m["name"] == st.get("model")), None)
            if not q or rm is None:
                ui.notify(_("Enter a question and pick a model first."), type="warning")
                return
            ex = experts.get_or_universal(user, fields["try_expert"].value)
            d = st["draft"] or {}
            tmp = skills.Skill(skill_id="trial", name=fields_val("name", d.get("name", "")),
                               description=fields_val("desc", d.get("description", "")),
                               body=fields_val("body", d.get("body", "")))
            block, _need = skills.prompt_block([tmp])
            st["busy"] = True
            run_btn.props("loading")
            for c in cols:
                c.set_content("…")
            try:
                a, b = await asyncio.gather(
                    skill_wizard.try_once(rm, user, token, ex, q, "", lang),
                    skill_wizard.try_once(rm, user, token, ex, q, block, lang),
                )
                cols[0].set_content(a or "–")
                cols[1].set_content(b or "–")
            finally:
                st["busy"] = False
                run_btn.props(remove="loading")

        run_btn.on_click(_run)

    def fields_val(key: str, fallback: str) -> str:
        el = fields.get(key)
        try:
            return el.value if el is not None and not el.is_deleted else fallback
        except AttributeError:
            return fallback

    def _keep_draft() -> None:
        if all(k in fields for k in ("name", "desc", "body")):
            try:
                st["draft"] = {"name": fields["name"].value, "description": fields["desc"].value,
                               "body": fields["body"].value}
            except Exception:
                pass

    async def _next() -> None:
        if st["busy"]:
            return
        s = st["step"]
        try:
            st["busy"] = True
            next_btn.props("loading")
            if s == 0:
                st["model"] = fields["model"].value
                if not st["model"]:
                    ui.notify(_("No model configured — add one in Settings > AI Models."), type="negative")
                    return
                set_pref("skill_wizard_model", st["model"])
            elif s == 1:
                fields["goal_v"], fields["example_v"] = fields["goal"].value or "", fields["example"].value or ""
                if not fields["goal_v"].strip():
                    ui.notify(_("Describe what the skill should do."), type="warning")
                    return
                status.set_text(_("The model is thinking of questions …"))
                try:
                    st["questions"] = await skill_wizard.interview(
                        st["model"], user, token, fields["goal_v"], fields["example_v"], lang)
                except Exception as exc:
                    log.warning("wizard interview failed", exc_info=True)
                    st["questions"] = []
                    ui.notify(_("The model could not ask questions ({err}) — continuing without.").format(
                        err=type(exc).__name__), type="warning")
            elif s == 2:
                answers = [(q["question"], inp.value or "") for q, inp in st["answers"]]
                for q, inp in st["answers"]:
                    q["answer"] = inp.value or ""
                status.set_text(_("The model is writing the draft …"))
                try:
                    st["draft"] = await skill_wizard.draft(
                        st["model"], user, token, fields["goal_v"], fields["example_v"], answers, lang)
                except Exception as exc:
                    log.warning("wizard draft failed", exc_info=True)
                    ui.notify(_("The model could not write a draft ({err}). Write it yourself below.").format(
                        err=type(exc).__name__), type="warning")
                    st["draft"] = {"name": "", "description": "", "body": ""}
            elif s == 3:
                _keep_draft()
            elif s == 4:
                d = st["draft"] or {}
                try:
                    sk = await skills.create(user, d.get("name", ""), d.get("description", ""), d.get("body", ""),
                                             created_with=st.get("model", ""))
                except skills.SkillError as err:
                    ui.notify(_("Another skill already has that name.") if str(err) == "duplicate"
                              else _("Give the skill a name."), type="warning")
                    return
                dlg.close()
                ui.notify(_("Skill saved. Assign it to an expert in its Skills tab."), type="positive")
                if on_saved:
                    await on_saved(sk)
                return
            st["step"] = min(4, s + 1)
            render()
        finally:
            st["busy"] = False
            status.set_text("")
            next_btn.props(remove="loading")

    def _back() -> None:
        if st["step"] == 3:
            _keep_draft()
        st["step"] = max(0, st["step"] - 1)
        render()

    next_btn.on_click(_next)
    back_btn.on_click(_back)
    render()
    dlg.open()


def _first_question(example: str) -> str:
    for line in (example or "").splitlines():
        line = line.strip()
        if line:
            return line.split(":", 1)[1].strip() if line.lower().startswith(("question:", "frage:")) else line
    return ""

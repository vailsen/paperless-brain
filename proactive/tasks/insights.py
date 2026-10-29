"""Phase 7 jobs: triage after each analysis, the deep dive (since Phase 8 a
Werkbank run in the app), the quiet-period digest."""

from __future__ import annotations

import logging
from datetime import timedelta

from proactive import clock, findings, interests, pushes, users
from proactive.jobs import Defer, Job, handler
from proactive.tasks import daily, documents

log = logging.getLogger("proactive.insights")


@documents.after_analysis
async def _triage(user, token, a, meta, model) -> bool:
    from proactive.triage import triage

    return await triage(user, token, a, meta, model)


#: How often the worker asks the app whether a deep dive's run is done, and
#: how long it keeps asking for a run nothing works on (the app restarted; the
#: user can resume it in the Werkbank).
DEEP_DIVE_POLL_S = 120
DEEP_DIVE_STALLED_POLL_S = 900
DEEP_DIVE_GIVE_UP_DAYS = 14


def _model_payload(model) -> dict:
    return {"name": model.name, "lane": model.lane, "source": model.source, "model_id": model.model_id}


@handler("deep_dive", needs_llm=True)
async def deep_dive(job: Job) -> None:
    """Start the research for an "Interesting" finding: a Werkbank run in the
    app (documents + web, guard active, steps aside for a chat). The run id goes
    into the finding at once — the finding page links to it while it runs — and
    `deep_dive_poll` picks the result up."""
    from proactive import jobs
    from proactive.internal_client import InternalUnavailable
    from proactive.internal_client import deep_dive as remote_start
    from proactive.models import resolve_model

    fid = job.payload["finding_id"]
    tok = users.token(job.user)
    if not tok:
        return
    f = findings.get(job.user, fid)
    if f is None or f.status != "interested":
        return
    model = resolve_model(job.user, tok)
    if model is None:
        raise Defer("no allowed model", 3600)
    run_id = f.research_run
    if not run_id:
        try:
            run_id = await remote_start(job.user, fid, _model_payload(model))
        except InternalUnavailable as exc:
            raise Defer(f"app not reachable ({exc})", 600) from exc
        await findings.update(job.user, fid, lambda x: {"research_run": run_id})
    jobs.enqueue(
        "deep_dive_poll", job.user,
        {"finding_id": fid, "run_id": run_id, "since": clock.now_iso()},
        delay=timedelta(seconds=DEEP_DIVE_POLL_S), dedupe_key=f"deep_dive_poll:{fid}",
    )


@handler("deep_dive_poll")
async def deep_dive_poll(job: Job) -> None:
    """Is the deep dive's run done? Then validate, write, send one message.

    Not an LLM job for the gate: the run itself is the app's work, and it keeps
    `app_busy` true for as long as it runs — a gated poll would never get to
    ask. The one model call (condensing the report) happens in the app, right
    after the run, on the host the run kept awake."""
    from proactive import analysis, notify, scoring
    from proactive.internal_client import InternalUnavailable
    from proactive.internal_client import deep_dive_result as remote_result
    from proactive.models import resolve_model
    from proactive.states import reactions_for, transition

    fid, run_id = job.payload["finding_id"], job.payload["run_id"]
    tok = users.token(job.user)
    if not tok:
        return
    f = findings.get(job.user, fid)
    if f is None or f.status != "interested" or f.research_run != run_id:
        return
    model = resolve_model(job.user, tok)
    if model is None:
        raise Defer("no allowed model", 3600)
    try:
        res = await remote_result(job.user, fid, run_id, _model_payload(model))
    except InternalUnavailable as exc:
        raise Defer(f"app not reachable ({exc})", 600) from exc
    state = res.get("state")
    if state == "gone":
        # The user deleted the run: the finding is interesting again, unresearched.
        log.info("%s: deep dive run %s of %s was deleted", job.user, run_id, fid)
        await findings.update(job.user, fid, lambda x: {"research_run": None} if x.research_run == run_id else None)
        return
    if state != "done":
        age = clock.utcnow() - clock.parse(job.payload.get("since") or clock.now_iso())
        if state == "stalled" and age > timedelta(days=DEEP_DIVE_GIVE_UP_DAYS):
            log.info("%s: deep dive run %s of %s stalled, no longer polled", job.user, run_id, fid)
            return
        raise Defer(f"run {run_id} {state}", DEEP_DIVE_POLL_S if state == "running" else DEEP_DIVE_STALLED_POLL_S)

    texts = {d: analysis.sidecar_text(d) or "" for d in f.doc_ids}
    rec, ev, src = scoring.validate_advice(
        res.get("recommendation", ""), res.get("evidence") or [], res.get("sources") or [],
        doc_texts=texts, fetched=res.get("fetched") or {},
    )
    from vault.research import wikilink

    note = str(res.get("note") or "")
    body = research_body(
        f.lang, run_id, findings.render_body(
            f.lang, summary=res.get("summary") or "", evidence=ev, sources=src, recommendation=rec,
        ), note=note,
    )
    original = f.body

    def mutate(x):
        nxt = transition(x.status, "researched")
        if not nxt:
            return None
        return {"status": nxt, "advice": bool(rec), **({"research_note": wikilink(note)} if note else {})}

    done = await findings.update(job.user, fid, mutate, body=lambda x: body + "\n---\n\n" + original)
    if done is None or done.status != "researched":
        return
    if note:
        from proactive import dossiers

        await dossiers.add_research(job.user, f.topic, note)
    from i18n import translator_for

    _ = translator_for(f.lang)
    # Its own title and push tag: with the finding's, the phone replaced the
    # finding's message in place — silently — and an email read like a repeat.
    notify.enqueue(notify.compose(
        job.user, f.lang, finding_id=fid, title=_("Research done: {title}").format(title=f.title),
        urgency=f.urgency, full_text=res.get("summary") or "",
        actions=reactions_for("researched", "insight"), counts=False, tag=f"{fid}-research",
    ))


def research_body(lang: str, run_id: str, sections: str, note: str = "") -> str:
    """The research result under its own heading, with a link to the full
    report — above the original finding, which stays below a rule.

    The link is an Obsidian link to the report's note in `PaperlessBrain
    Research/`: it works in Obsidian and opens the PDF on the findings page,
    and it outlives the Werkbank run. Without a note (it could not be
    written) the run's board stands in."""
    from i18n import translator_for
    from proactive.notify import app_link
    from vault.research import wikilink

    _ = translator_for(lang)
    head = _("Research result ({date})").format(date=clock.local_now().date().isoformat())
    if note:
        link = wikilink(note, _("Full report"))
    else:
        link = _("[Full report in deep research]({url})").format(url=app_link(f"/werkbank?run={run_id}"))
    # The run's sections one level down, under the research heading.
    demoted = "\n".join("#" + line if line.startswith("## ") else line for line in sections.splitlines())
    return f"## {head}\n\n{link}\n\n{demoted}\n"


@daily.also
async def quiet_digest(user: str) -> None:
    """No message for `PROACTIVE_QUIET_DAYS` → one digest with the top held
    findings. Counts once. Also how low-weight topics get a second chance."""
    from datetime import timedelta

    from config.proactive import proactive_settings
    from i18n import translator_for
    from proactive import notify, prefs
    from proactive.states import transition

    cfg = proactive_settings()
    if cfg.proactive_digest_held_items <= 0:
        return
    last = pushes.last_push(user)
    quiet_since = clock.utcnow() - timedelta(days=cfg.proactive_quiet_days)
    if last and clock.parse(last) > quiet_since:
        return
    held = sorted(
        (f for f in findings.list_findings(user) if f.status == "held" and f.kind != "deadline"),
        key=lambda f: f.priority, reverse=True,
    )[: cfg.proactive_digest_held_items]
    if not held:
        return
    lang = prefs.get(user).content_language
    _ = translator_for(lang)
    for f in held:
        await findings.update(user, f.finding_id, lambda x: (
            {"status": "surfaced", "surfaced_at": clock.local_now().isoformat(timespec="seconds")}
            if transition(x.status, "digest") else None
        ))
        if f.topic:
            await interests.record(user, f.topic, "surfaced")
    lines = [f"- {f.title}: {notify.finding_link(f.finding_id)}" for f in held]
    n = notify.compose(
        user, lang, finding_id="", title=_("What you may have missed"),
        urgency="low", note="\n".join(lines), counts=False,
    )
    if notify.enqueue(n):
        pushes.record(user, "digest")
    log.info("%s: digest with %d held finding(s)", user, len(held))

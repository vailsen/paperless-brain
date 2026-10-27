"""Phase 7 jobs: triage after each analysis, the deep dive (since Phase 8 a
Werkbank run in the app), the quiet-period digest."""

from __future__ import annotations

import logging

from proactive import clock, findings, interests, pushes, users
from proactive.jobs import Defer, Job, handler
from proactive.tasks import daily, documents

log = logging.getLogger("proactive.insights")


@documents.after_analysis
async def _triage(user, token, a, meta, model) -> bool:
    from proactive.triage import triage

    return await triage(user, token, a, meta, model)


@handler("deep_dive", needs_llm=True)
async def deep_dive(job: Job) -> None:
    """`run_deep_dive(finding)`: research in the app (documents + web, guard
    active), validated and written here, then one message (it was asked for,
    so it does not count against the weekly limit)."""
    from proactive import analysis, notify, scoring
    from proactive.internal_client import InternalUnavailable
    from proactive.internal_client import deep_dive as remote
    from proactive.models import resolve_model
    from proactive.states import reactions_for, transition

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
    try:
        res = await remote(job.user, fid, {
            "name": model.name, "lane": model.lane, "source": model.source, "model_id": model.model_id,
        })
    except InternalUnavailable as exc:
        raise Defer(f"app not reachable ({exc})", 600) from exc
    texts = {d: analysis.sidecar_text(d) or "" for d in f.doc_ids}
    rec, ev, src = scoring.validate_advice(
        res.get("recommendation", ""), res.get("evidence") or [], res.get("sources") or [],
        doc_texts=texts, fetched=res.get("fetched") or {},
    )
    body = findings.render_body(
        f.lang, summary=res.get("summary") or "", evidence=ev, sources=src, recommendation=rec,
    )
    original = f.body

    def mutate(x):
        nxt = transition(x.status, "researched")
        return {"status": nxt, "advice": bool(rec)} if nxt else None

    done = await findings.update(job.user, fid, mutate, body=lambda x: body + "\n---\n\n" + original)
    if done is None or done.status != "researched":
        return
    notify.enqueue(notify.compose(
        job.user, f.lang, finding_id=fid, title=f.title, urgency=f.urgency,
        full_text=res.get("summary") or "", actions=reactions_for("researched", "insight"),
        counts=False,
    ))


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
    if cfg.proactive_digest_held_items <= 0 or not pushes.allowed(user):
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

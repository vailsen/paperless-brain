"""The dashboard's compute line under "Ollama": generating or idle, what is in
memory, and how fast the last generation was. Pure formatting — the data
comes from `services.inference_activity` and Ollama's `/api/ps`."""

from __future__ import annotations


from i18n import N_
from services.inference_activity import cpu_share

PURPOSE_LABELS = {
    "chat": N_("Chat"),
    "ingest": N_("Ingest"),
    "werkbank": N_("Deep research"),
    "proactive": N_("Proactive"),
    "background": N_("Background task"),
}


def _duration(secs: float) -> str:
    secs = int(max(secs, 0))
    return f"{secs // 60}:{secs % 60:02d}"


def lines(_, models: list[dict] | None, snap: dict, now_ts: float) -> list[tuple[str, str]]:
    """(icon, text) rows, most important first."""
    out: list[tuple[str, str]] = []
    running = snap.get("running") or []
    if running:
        first = running[0]
        text = _("Generating — {what} · {model} · {time}").format(
            what=_(PURPOSE_LABELS.get(first.get("purpose", ""), PURPOSE_LABELS["background"])),
            model=first.get("model", ""), time=_duration(now_ts - float(first.get("since") or now_ts)),
        )
        if len(running) > 1:
            text += " · " + _("{n} more").format(n=len(running) - 1)
        out.append(("bolt", text))
    elif models is not None:
        out.append(("hourglass_empty", _("Idle")))
    if models:
        parts = []
        for m in models:
            p = m.get("name") or m.get("model") or "?"
            vram = m.get("size_vram") or 0
            if vram:
                p += " · " + _("{gb} GB").format(gb=f"{vram / 1e9:.1f}")
            # Partly in system RAM: the usual reason a fast model crawls.
            cpu = cpu_share(m)
            if cpu >= 5:
                p += " · " + _("CPU share: {pct} %").format(pct=cpu)
            parts.append(p)
        out.append(("memory", _("In memory: {models}").format(models="; ".join(parts))))
    elif models is not None:
        out.append(("memory", _("No model in memory")))
    last = snap.get("last")
    if last:
        ago = max(0, int(now_ts - float(last.get("at") or now_ts)))
        when = _("just now") if ago < 60 else (
            _("{n} min ago").format(n=ago // 60) if ago < 3600 else _("{n} h ago").format(n=ago // 3600))
        rate = f"{last.get('tps', 0):.0f}"
        if last.get("kind") == "estimate":
            rate = "≈ " + rate  # the prompt's reading time is in it
        out.append(("speed", _("Last: {rate} tokens/s · {what} · {when}").format(
            rate=rate, what=_(PURPOSE_LABELS.get(last.get("purpose", ""), PURPOSE_LABELS["background"])),
            when=when)))
    return out

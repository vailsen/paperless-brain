"""One-time merge of findings that belong together (written before findings
were grouped by sender): twelve Vodafone invoices, each announcing the same
price change, were twelve findings.

Per user, once (`kv` marker): every insight learns its documents' sender
(Paperless, as the user), then findings with the same (topic, sender) — or the
same sender and the same title — become one: the oldest stays, the others'
documents and quotes join it, their files are deleted (the vault's git keeps
them). The merged finding takes the most engaged status of the group, so a
finding the user marked "interesting" is not lost in a merge.
"""

from __future__ import annotations

import logging

from proactive import findings

log = logging.getLogger("proactive.series")

MARKER = "series_v1:{user}"

_ENGAGEMENT = {
    "researched": 7, "interested": 6, "surfaced": 5, "snoozed": 4,
    "held": 3, "new": 2, "done": 1, "dismissed": 0,
}


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


async def consolidate(user: str, token: str) -> int:
    """Merge the user's split series. Returns how many files were merged away."""
    from proactive.db import kv_get, kv_set
    from proactive.paperless_ro import TokenRejected, document
    from vault.notes import delete

    marker = MARKER.format(user=user)
    if kv_get(marker):
        return 0
    items = [f for f in findings.list_findings(user) if f.kind == "insight" and f.doc_ids]
    for f in items:
        if f.correspondent is None:
            try:
                meta = await document(token, f.doc_ids[0]) or {}
            except TokenRejected:
                return 0  # try again once the user has signed in
            except Exception as exc:  # Paperless unreachable: next scan
                log.info("%s: series merge postponed — Paperless: %s", user, type(exc).__name__)
                return 0
            c = meta.get("correspondent")
            f.correspondent = c if isinstance(c, int) else None

    # Union-find over two keys: (topic, sender) and (sender, title).
    parent = {f.finding_id: f.finding_id for f in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first: dict[tuple, str] = {}
    for f in items:
        if f.correspondent is None:
            continue
        for key in (("t", f.topic, f.correspondent), ("n", _norm(f.title), f.correspondent)):
            if key in first:
                parent[find(f.finding_id)] = find(first[key])
            else:
                first[key] = f.finding_id
    groups: dict[str, list[findings.Finding]] = {}
    for f in items:
        groups.setdefault(find(f.finding_id), []).append(f)

    removed = 0
    for group in groups.values():
        group.sort(key=lambda f: (f.created or "", f.finding_id))
        head = group[0]
        key = findings.series_key(head.topic, head.correspondent, head.doc_ids[0])
        best = max(group, key=lambda f: _ENGAGEMENT.get(f.status, 2)).status
        if len(group) == 1:
            if head.correspondent is not None and head.finding_key != key:
                await findings.update(user, head.finding_id, lambda _x: {
                    "finding_key": key, "correspondent": head.correspondent})
            continue
        for other in group[1:]:
            other.finding_key, other.correspondent = key, head.correspondent
            head, _outcome = await findings.add_to_series(user, head, other)
        await findings.update(user, head.finding_id, lambda x: (
            {"status": best} if x.status != best else None))
        for other in group[1:]:
            await delete(user, other.rel)
            findings._index.pop(other.finding_id, None)
            removed += 1
        log.info("%s: merged %d findings into %s (%s)", user, len(group), head.finding_id, head.title)
    kv_set(marker, "1")
    return removed

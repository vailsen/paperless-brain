# PaperSage — Vault-Backed Memory & Knowledge Base

**Status:** design complete, ready for implementation
**Scope:** replace the Chroma-direct memory store with a Markdown-backed source of truth, and add the user's Obsidian vault as a separate knowledge base.

---

## 1. Motivation

Today the agent brain stores memory **content directly in ChromaDB**. This violates the
project's core invariant — the same one already enforced for documents:

> ChromaDB holds only the index. The authoritative, mutable content lives in an external
> source of truth (Paperless for documents).

This feature brings memory into line: **Markdown files on disk are the source of truth;
ChromaDB is only the embedding index.** Benefits: re-embeddable on model change, portable,
human-editable, version-controlled, and optionally visible in Obsidian.

Obsidian is an *optional viewer on top*, not the core. The core value is "files as source of
truth." This matters because most of the ~40 users will never open Obsidian.

---

## 2. Two separate concerns — do NOT merge them

There are two systems here. Keep them in **separate Chroma collections** with **separate tools**.

| | **Agent Brain** | **Vault Knowledge Base** |
|---|---|---|
| What | Curated, atomic facts the agent learned | The user's own notes / daily notes / project docs |
| Author | Agent (CRUD), optionally the user | User only (Obsidian) |
| Collection | `brain` (existing) | `vault` (new) |
| File→embedding | 1 fact = 1 `.md` = 1 embedding (no chunking) | 1 file = N chunks (markdown-aware chunking) |
| Location | `<vault>/PaperSage Memory/` subfolder | everything else in the vault |
| PaperSage access | read + write | read-only (ingest only) |
| Tool | `brain_search` (existing) | `vault_search` (new) |

Rationale for the split: when the agent recalls "what do I know about this user," it must get
its curated facts back — not wade through 500 daily notes. Merging dilutes brain precision
exactly where it matters. Separate collections + separate tools means the LLM **explicitly
chooses** "recall what I know" vs. "search the user's notes."

**One vault watcher, two routing targets.** Path under `PaperSage Memory/` → `brain`;
everything else → `vault`. Same plumbing, the split is only at the embedding target.

---

## 3. File format

Every managed/ingested `.md` carries YAML frontmatter; the body is the embedded text.

Brain fact (`<vault>/PaperSage Memory/prefers-webdav.md`):

```markdown
---
psage_id: 0f3c8a1e-...
created: 2026-06-14T10:21:00Z
updated: 2026-06-14T10:21:00Z
source: conversation        # conversation | document | manual
tags: [infra, preferences]
---
The user prefers self-hosted WebDAV over OneDrive for Obsidian sync.
```

- **`psage_id`** is the stable identity (a UUID). It survives renames/moves — critical in a
  user-managed Obsidian vault where files get shuffled constantly.
- The **filename is cosmetic.** Identity is `psage_id`, never the path. The agent may pick a
  readable slug; dedupe with a numeric suffix on collision.
- For brain files: body = the single fact, embedded as-is.
- For vault files: arbitrary user content, chunked on ingest (see §5).

A user who hand-creates a brain `.md` without `psage_id` → PaperSage assigns one on next sync
and writes it back to frontmatter (idempotent: next sync sees the id, no churn).

---

## 4. Change detection = git (no hashing, no SQLite manifest)

Git is content-addressed — hashing is its job. It is simultaneously the **change detector**,
the **manifest**, and a **free audit trail + rollback**. We do not build any of those.

**Setup (per user vault, once):**
- `git init` in the vault root if absent.
- `.gitignore`: ignore everything except ingestible types. Practically: ignore all, then
  un-ignore `*.md` (and any other types we choose to ingest). This keeps Obsidian image/PDF
  attachments out of the repo (no bloat) and keeps the diff limited to what we care about.
- `.git/` is **excluded from Remotely Save** (client-side ignore) so the repo never propagates
  to client workstations. The repo is server-side only.
- Initial `git add -A && git commit`, then full ingest of every tracked `.md`.

**Sync cycle (incremental):**
```
1. git -C <vault> diff --name-status HEAD     # A / M / D / R per file
2. for each change:
     route by path prefix (PaperSage Memory/ → brain, else → vault)
     A or M → (re)embed and upsert into the target collection
     D       → delete entries by `path` metadata (see §6)
     R       → if 100% similarity: update `path` metadata only, no re-embed
               else: treat as D(old) + A(new)
3. git -C <vault> add -A && git commit -m "psage sync <ts>"
```

**Commit is last, on purpose** — it is the bookmark meaning "processed up to here." If we crash
between step 2 and 3, the next cycle re-detects the same changes and re-embeds idempotently.
Crash safety for free.

Use plain `git` via `subprocess` (`git -C <path> ...`). **No GitPython** — zero new deps,
full control, matches the project's dependency-minimalism. Ensure the `git` binary exists in
the LXC.

---

## 5. Embedding & chunking

Reuse the **existing** embedding helper (`intfloat/multilingual-e5-large-instruct`). It already
applies the e5 instruct convention — do not reinvent it. Passages (brain facts, vault chunks)
are embedded as documents; `brain_search` / `vault_search` queries use the instruct query prefix.

- **Brain:** no chunking. File = chunk = embedding.
- **Vault:** markdown-aware chunking — split on heading hierarchy, fall back to paragraph, with
  a max token budget. Store `chunk_index` and the heading path as metadata. This is the same
  chunk-from-source pattern already used for Paperless documents.

---

## 6. Chroma metadata schema

**`brain` collection** (keyed by `psage_id`):
```
id        = psage_id
metadata  = { psage_id, path, source, tags, created, updated }
document  = fact text
```

**`vault` collection** (keyed by `psage_id:chunk_index`):
```
id        = f"{psage_id}:{chunk_index}"
metadata  = { psage_id, path, chunk_index, heading_path, updated }
document  = chunk text
```

Storing **both `psage_id` and `path`** matters: identity/updates go through `psage_id`, but
deletions are driven by git's `D <path>` — and a deleted file's frontmatter is gone, so we
delete via `where path == <path>` instead. That's why `path` lives in metadata even though
`psage_id` is the primary key.

`psage_id → path` lookup (for update/delete by id) = a metadata query on Chroma; no separate
index store.

---

## 7. Sync trigger: on user interaction, NOT time-based

**No background scheduler.** Sync for a given user runs when **that user interacts** with
PaperSage, before any `brain_search` / `vault_search` retrieval in the turn.

Guards:
- **Per-user `asyncio.Lock`** around the whole git + Chroma mutation critical section.
- **At most once per turn:** a per-turn flag (or a short cooldown, e.g. 2–5 s) so a rapid
  multi-tool turn doesn't re-run the sync on every tool call.
- A **manual "Sync now" button** on the dashboard triggers the same path on demand.

`git diff` on an unchanged tree is near-instant, so the common case (nothing changed) costs
milliseconds. Cost is only incurred for actual deltas.

**Agent brain writes are synchronous:** when the agent creates/updates/deletes a memory, the
write tool writes the `.md`, embeds, and commits **inline** — so the agent sees its own new
memory immediately, not at the next sync. Same `embed + commit` primitive, just a different
trigger. Brain writes take the **same per-user lock** as the interaction sync.

---

## 8. Topology & per-user isolation

- Each user's vault is their own per-user bind mount: `/mnt/vaults/<username>` (existing
  mechanism — reuse it, don't build a new one). The brain subfolder is
  `/mnt/vaults/<username>/PaperSage Memory/`. Filesystem-level isolation between users.
- **One authoritative copy. Direct NAS bind mount. Do NOT sync a local copy** — that would be
  bidirectional sync (brain writes would have to propagate back to NAS), i.e. the exact
  LiveSync/CouchDB complexity class already rejected on principle.
- **Critical invariant:** the WebDAV endpoint that Remotely Save targets must serve the **same
  directory** that PaperSage bind-mounts. Otherwise there are two copies and a sync gap.
- NFS caveats: tune attribute caching (`actimeo`) so the scanner doesn't read stale mtimes;
  keep atomic writes (temp + rename) **within** the mount.
- **Escape hatch (do NOT build preemptively):** if the scan/git gets slow on a large vault,
  move the authoritative copy to the LXC SSD, serve Obsidian via WebDAV from there, and demote
  the NAS to a nightly backup target.
- The brain `.md` files are **always written** (filesystem is the SoT) regardless of whether
  the user uses Obsidian. Obsidian visibility is an optional overlay, not a precondition.

---

## 9. Concurrency

All git + Chroma mutations for a user's vault are serialized by that user's `asyncio.Lock`.
This covers (a) the interaction-triggered sync and (b) synchronous brain writes — they must not
collide (`.git/index.lock`). Writes are atomic (temp file + rename within the mount). Remotely
Save conflict files (`*.conflict.md`) are ignored at ingest.

---

## 10. Tool & UI changes

**LLM tools:**
- **Read tools unchanged** (`brain_search` queries Chroma exactly as today). New `vault_search`
  mirrors it against the `vault` collection.
- **Write tools (brain create/update/delete) re-implemented.** Functionally identical from the
  agent's perspective; implementation changes from "direct Chroma upsert" to
  "write/delete `.md` → embed → commit." (See §7.)
  - `create_memory(text, tags?)`: gen `psage_id`, write file, embed, upsert, commit.
  - `update_memory(psage_id, text)`: resolve path via metadata, rewrite body + `updated`,
    re-embed, upsert, commit.
  - `delete_memory(psage_id)`: resolve path, `rm`, delete Chroma entry, commit.

**Frontend:**
- **Remove** the old Chroma-direct "Gedächtnis" module.
- **Replace with a slim, file-backed brain viewer/editor** that reads `PaperSage Memory/` and
  whose edit/delete actions write the file + re-embed (the same write primitive). This exists
  because most users won't open Obsidian and still need to inspect/correct what the agent thinks
  it knows. **Brain only** — the user vault knowledge base is managed in Obsidian, not in this UI.

---

## 11. Rejected alternatives

- **SQLite sync manifest** — over-engineered; git's index *is* the manifest. SQLite re-enters
  only for the future document-lineage / `document_edges` feature (genuinely relational). Do not
  couple sync to it.
- **Custom content hashing** — git is already content-addressed; redundant.
- **Sync a local copy to the LXC SSD** — reintroduces bidirectional sync (rejected class).
- **Merging brain + vault into one collection** — dilutes brain retrieval precision.
- **Removing the memory UI entirely** — strands the ~majority of users who don't use Obsidian.
- **GitPython / python-frontmatter as hard deps** — prefer `subprocess` git + a tiny
  YAML-frontmatter parse (PyYAML already in the stack). Optional, implementer's call, but bias
  to zero new deps.
- **Time-based / scheduled sync** — replaced by on-interaction trigger.

---

## 12. Edge cases checklist

- [ ] File renamed/moved in Obsidian → `psage_id` keeps identity; update `path` metadata only.
- [ ] Hand-created brain file without `psage_id` → assign + write back to frontmatter.
- [ ] File deleted → delete Chroma entries by `path` metadata (frontmatter is gone).
- [ ] File half-written during scan → committed as-is; next edit → next diff → re-embed.
- [ ] Remotely Save `*.conflict.md` → ignored at ingest.
- [ ] First run for a user → `git init` + `.gitignore` + baseline commit + full ingest.
- [ ] User with no vault configured → brain still written to the per-user managed dir; Obsidian
      overlay simply absent.

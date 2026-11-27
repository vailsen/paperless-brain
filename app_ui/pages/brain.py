"""Vault note explorer — file tree, markdown editor, frontmatter properties.

This page replaces the old fact/deadline card UI. The reason is practical: the
vault holds far more than agent-curated facts (to-do notes, memos, logs), and
until now the only ways to edit any of it were shell access on the server or an
Obsidian client mounted over WebDAV.

Three things about how it works are easy to undo by accident:

- **It writes files and nothing else.** No Chroma call, no git commit. Indexing
  stays with `vault.sync.sync_user()`, which runs at the start of every chat
  turn — so an edit is always indexed before the retrieval that needs it, and
  an editing session costs one commit instead of one per keystroke.
- **The editor holds the body; the frontmatter lives in `st["fm_raw"]`.** The
  `---` block is edited only through the properties panel, because two editable
  copies of the same bytes drift apart. `_compose()` is the single place they
  are put back together — for saving, for the dirty check, for the merge.
- **Dirty state is `_compose() != base`, not a flag.** Assigning `editor.value`
  server-side (reload, merge, properties edit) fires the same change handler as
  typing; comparing against the last known file text is what keeps that from
  marking fresh content dirty, and it is immune to a client echo that a boolean
  guard would not survive.
- **The frontmatter path is `vault/note_text.py`, never `frontmatter.write`.**
  The latter re-dumps the whole YAML block, so an autosave of an untouched note
  would rewrite comments, key order and scalar formatting.
"""

import asyncio
import difflib
import time
from datetime import datetime
from pathlib import Path

import yaml
from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from app_ui.note_voice_dialog import create_note_voice_dialog, note_voice_enabled
from app_ui.theme import current_theme
from app_ui.vault_editor import VaultEditor
from app_ui.vault_routes import FILE_PATH
from config.settings import settings
from i18n import get_translator
from services.markdown_text import escape_intraword_underscores
from services.user_prefs import get_pref, set_pref
from vault import links as vlinks
from vault import managed, notes
from vault import scan as vscan
from vault import search as vsearch
from vault.frontmatter import ID_KEY, sanitize_tags
from vault.note_text import (
    NoteText,
    apply_edits,
    blocks_are_faithful,
    join_note,
    properties,
    raw_blocks,
    split_note,
)
from vault.router import is_brain_path

AUTOSAVE_IDLE_S = 2.0
TICK_S = 0.5
POLL_EVERY_TICKS = 10          # ≈5 s between external-change probes
MOBILE_BP = 900                # keep in step with the media query below
# Properties the panel renders with a dedicated widget instead of a text field.
BOOL_KEYS = ("dont_ingest", "common")
LIST_KEYS = ("tags",)
# Tree ids of the read-only "Shared by others" section. `:` cannot occur in a
# vault path (notes.check_name refuses it, and so does Obsidian), so no real
# note can collide with this prefix.
SHARED_ROOT = "::shared"

_PAGE_CSS = """
<style>
.vault-wrap {
  display: flex; align-items: stretch; gap: 0;
  height: calc(100dvh - 118px); min-height: 380px; width: 100%; min-width: 0;
}
.vault-tree-pane {
  width: 330px; flex-shrink: 0; min-width: 0;
  display: flex; flex-direction: column;
  background: var(--c-surface); border: 1px solid var(--c-border);
  border-radius: 8px 0 0 8px; overflow: hidden;
}
.vault-main {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  background: var(--c-surface); border: 1px solid var(--c-border);
  border-left: none; border-radius: 0 8px 8px 0; overflow: hidden;
}
.vault-scrim { display: none; }

/* Tree nodes: neutral icons, one line, ellipsis rather than a wider pane. */
.vault-node { display: flex; align-items: center; gap: 5px; min-width: 0; width: 100%; padding: 1px 0; }
.vault-node-icon { color: var(--c-text-muted); flex-shrink: 0; }
.vault-node-label {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 0.75rem; color: var(--c-text-2); min-width: 0;
}
/* The selected node is the one whose full name matters — let it wrap instead
   of hiding the half that identifies it. */
.vault-tree-pane .q-tree__node-header.q-tree__node--selected .vault-node-label {
  white-space: normal; overflow: visible; overflow-wrap: anywhere;
}
.vault-node-att .vault-node-label { color: var(--c-text-muted); font-style: italic; }
/* Genuine state the user may want to act on: written but not yet indexed. */
.vault-pending-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--c-warn); flex-shrink: 0; margin-left: auto;
}
.vault-tree-pane .q-tree__node-header { padding: 1px 4px; min-height: 0; }
/* Quasar indents each level by a wide fixed padding and spaces rows
   generously; a file tree wants density and the name is what matters. */
.vault-tree-pane .q-tree__children { padding-left: 14px; }
.vault-tree-pane .q-tree__node { padding-bottom: 0; }
.vault-tree-pane .q-tree__arrow { margin-right: 2px; font-size: 18px; }
.vault-tree-pane .q-tree__node--child > .q-tree__node-header { margin-top: 0; }
/* Quasar sizes a node header to its content, so a long note name widens the
   whole tree past the pane and the label's ellipsis never engages. min-width:0
   at every level of the flex chain is what gives it something to truncate
   against. */
.vault-tree-pane .q-tree,
.vault-tree-pane .q-tree__node,
.vault-tree-pane .q-tree__node-header,
.vault-tree-pane .q-tree__node-header-content { min-width: 0; }
.vault-tree-pane .q-tree__node-header-content { overflow: hidden; }
/* The scroll area's content box is a flex container, so the tree is a flex
   item sized from its max-content — an explicit width is what pins it to the
   pane instead. */
.vault-tree-pane .q-tree { width: 100%; max-width: 100%; }
.vault-tree-pane .q-tree__arrow { color: var(--c-text-muted); }
/* Selection is what the toolbar acts on and what a second tap opens, so it has
   to be unmistakable: a full outline, in the active-selection accent — the one
   meaning the accent still carries. `outline` rather than `border` because it is
   drawn outside the box model and so cannot shift the row by a pixel.

   NOTE the selector: Quasar puts `q-tree__node--selected` on the *header*, not
   on the `q-tree__node` wrapper it reads like it belongs to. Targeting the
   wrapper matches nothing and the selection silently has no styling at all. */
.vault-tree-pane .q-tree__node-header.q-tree__node--selected .vault-node-label { color: var(--c-text); }
.vault-tree-pane .q-tree__node-header.q-tree__node--selected .vault-node-icon { color: var(--c-accent); }
.vault-tree-pane .q-tree__node-header.q-tree__node--selected {
  background: var(--c-surface-2); border-radius: 4px;
  outline: 1.5px solid var(--c-accent); outline-offset: -1.5px;
}
/* Stops the browser reading a quick second tap as a zoom gesture and eating
   it — the second tap is how a note is opened. */
.vault-tree-pane { touch-action: manipulation; }

/* Tree: one tap opens; ⋯ (hover on a desktop, always on a phone) for the rest. */
.vault-node-more { margin-left: 2px; color: var(--c-text-muted); opacity: 0; flex-shrink: 0; }
.vault-tree-pane .q-tree__node-header:hover .vault-node-more { opacity: 1; }
@media (hover: none) { .vault-node-more { opacity: .7; } }
.vault-node-lock { color: var(--c-text-muted); flex-shrink: 0; }
.vault-node.is-drop { outline: 1.5px dashed var(--c-accent); outline-offset: 1px; border-radius: 4px; }
.vault-hit { padding: 6px 10px; border-radius: 6px; cursor: pointer; display: flex; flex-direction: column;
  gap: 1px; min-width: 0; margin: 1px 4px; }
.vault-hit:hover { background: var(--c-surface-2); }
.vault-hit .t { font-size: .82rem; color: var(--c-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vault-hit .p { font-size: .7rem; color: var(--c-text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vault-hit .s { font-size: .74rem; color: var(--c-text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vault-hit mark { background: var(--c-hl); color: inherit; border-radius: 2px; }
.vault-results .q-scrollarea__content { max-width: 100%; }
.vault-crumbs { overflow: hidden; }
.vault-crumb, .vault-crumb-sep { font-size: .78rem; color: var(--c-text-muted); white-space: nowrap; flex-shrink: 1;
  overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.vault-crumb-sep { flex-shrink: 0; }
.vault-crumb-name { font-size: .875rem; font-weight: 600; color: var(--c-text); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; min-width: 0; cursor: text; flex-shrink: 2; }
.vault-lock { padding: 4px 10px; border-bottom: 1px solid var(--c-border); background: var(--c-surface-2);
  color: var(--c-text-2); flex-shrink: 0; }
.vault-format { display: none; padding: 0 4px; border-bottom: 1px solid var(--c-border); flex-shrink: 0;
  overflow-x: auto; scrollbar-width: none; }
.vault-chip { font-size: .75rem; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--c-border);
  color: var(--c-text-2); background: var(--c-surface-2); cursor: pointer; }
.vault-backlinks { border-top: 1px solid var(--c-border); flex-shrink: 0; max-height: 40%; overflow-y: auto; }

/* Editor: Obsidian-style live preview (app_ui/vault_editor). */
.vault-editor { flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
.vault-editor .pbe-root { flex: 1; min-height: 0; overflow: hidden; display: flex; }
.vault-editor .pbe-root .cm-editor { flex: 1; min-width: 0; }
.vault-editor .cm-content { margin: 0 auto; }
.pbe-h { color: var(--c-text); font-weight: 600; }
.pbe-h1 { font-size: 1.55em; line-height: 1.35; } .pbe-h2 { font-size: 1.3em; } .pbe-h3 { font-size: 1.12em; }
.pbe-strong { font-weight: 700; color: var(--c-text); } .pbe-em { font-style: italic; }
.pbe-strike { text-decoration: line-through; color: var(--c-text-muted); }
.pbe-code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88em;
  background: var(--c-surface-2); border-radius: 3px; padding: 0 3px; }
.pbe-codeblock { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em;
  background: var(--c-surface-2); }
.pbe-quote { border-left: 3px solid var(--c-border-strong); padding-left: 10px !important; color: var(--c-text-muted); }
.pbe-link, .pbe-wiki { color: var(--c-accent-text); cursor: pointer; text-decoration: underline;
  text-decoration-color: var(--c-accent-border); text-underline-offset: 2px; }
.pbe-unresolved { color: var(--c-text-muted); text-decoration-style: dashed; }
.pbe-tag { color: var(--c-accent-text); background: var(--c-accent-soft); border-radius: 999px; padding: 0 5px;
  cursor: pointer; font-size: .92em; }
.pbe-check { width: 15px; height: 15px; margin: 0 6px 0 0; vertical-align: -2px; accent-color: var(--c-accent);
  cursor: pointer; }
.pbe-done { color: var(--c-text-muted); text-decoration: line-through; }
.pbe-bullet { color: var(--c-text-muted); padding-right: 2px; }
.pbe-hr { border-top: 1px solid var(--c-border-strong); margin: 10px 0; }
.pbe-img { max-width: 100%; max-height: 420px; border-radius: 6px; display: block; margin: 6px 0; }
.pbe-table-wrap { overflow-x: auto; max-width: 100%; margin: 4px 0; cursor: text; }
.pbe-table { border-collapse: collapse; font-size: .88em; }
.pbe-table th { background: var(--c-surface-2); color: var(--c-text); text-align: left; font-weight: 600; }
.pbe-table th, .pbe-table td { border: 1px solid var(--c-border); padding: 4px 9px; min-width: 4rem; }
.vault-editor.is-source .cm-scroller { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .84rem; }
.vault-id { font-family: ui-monospace, monospace; font-size: 0.75rem; color: var(--c-text-muted); overflow-wrap: anywhere; }

/* Rendered preview — same containment rules as the chat bubbles, for the same
   reason: a wide table must scroll inside its box, never widen the page. */
.note-md { color: var(--c-text-2); font-size: 0.9rem; line-height: 1.65; max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
.note-md h1, .note-md h2, .note-md h3 { color: var(--c-text); font-weight: 600; margin: 0.9rem 0 0.4rem 0; }
.note-md h1 { font-size: 1.25rem; } .note-md h2 { font-size: 1.05rem; } .note-md h3 { font-size: 0.95rem; }
.note-md p { margin: 0 0 0.6rem 0; }
.note-md ul, .note-md ol { padding-left: 1.4rem; margin: 0 0 0.6rem 0; }
.note-md li { margin-bottom: 0.2rem; }
.note-md li:has(> input[type="checkbox"]) { list-style: none; margin-left: -1.15rem; }
.note-md input[type="checkbox"] { margin-right: 0.45rem; vertical-align: -0.1em; accent-color: var(--c-accent); }
.note-md code { background: var(--c-border); border-radius: 3px; padding: 0.1rem 0.3rem; font-family: monospace; font-size: 0.85em; }
.note-md pre { background: var(--c-bg); border: 1px solid var(--c-border); border-radius: 6px; padding: 0.75rem; overflow-x: auto; }
.note-md blockquote { border-left: 3px solid var(--c-border-strong); padding-left: 0.75rem; color: var(--c-text-muted); }
.note-md table { display: block; overflow-x: auto; width: max-content; max-width: 100%; border-collapse: collapse; font-size: 0.85em; }
.note-md th { background: var(--c-border); color: var(--c-text-muted); padding: 4px 8px; text-align: left; }
.note-md td { border-top: 1px solid var(--c-border); padding: 4px 8px; }
.note-md td, .note-md th { overflow-wrap: normal; min-width: 5rem; }

/* Quasar positions the scroll-area content box absolutely with min-width:100%,
   so without this cap one wide row makes the whole list scroll sideways. */
.vault-tree-pane .q-scrollarea__content, .vault-props .q-scrollarea__content { max-width: 100%; }

.vault-conflict {
  background: var(--c-warn-bg); border: 1px solid var(--c-warn);
  border-radius: 6px; padding: 8px 10px; margin: 8px 10px 0 10px;
}
.vault-diff {
  font-family: ui-monospace, monospace; font-size: 0.72rem; white-space: pre;
  color: var(--c-text-2); background: var(--c-bg); border-radius: 4px;
  padding: 6px 8px; max-height: 160px; overflow: auto;
}

@media (max-width: 900px) {
  .vault-format { display: flex; }
  .vault-wrap { height: calc(100dvh - 104px); }
  .vault-tree-pane {
    position: fixed; top: var(--q-header-height, 52px); bottom: 0;
    width: min(88vw, 330px); left: calc(-1 * min(88vw, 330px) - 4px);
    z-index: 501; border-radius: 0; box-shadow: 4px 0 24px rgba(0,0,0,.5);
    transition: left .25s cubic-bezier(.4,0,.2,1);
  }
  .vault-tree-pane.open { left: 0; }
  .vault-scrim {
    position: fixed; top: var(--q-header-height, 52px); left: 0; right: 0; bottom: 0;
    z-index: 500; background: rgba(0,0,0,.5);
  }
  .vault-scrim.open { display: block; }
  .vault-main { border-left: 1px solid var(--c-border); border-radius: 8px; }
}
</style>
"""

_NODE_TEMPLATE = """
<div class="vault-node" :class="props.node.note ? '' : (props.node.dir ? '' : 'vault-node-att')"
     :draggable="!props.node.root && !String(props.node.id).startsWith('::')"
     @dragstart="e => e.dataTransfer.setData('text/pb-node', props.node.id)"
     @dragover.prevent="e => e.currentTarget.classList.add('is-drop')"
     @dragleave="e => e.currentTarget.classList.remove('is-drop')"
     @drop.prevent.stop="e => { e.currentTarget.classList.remove('is-drop');
        $parent.$emit('node_drop', {from: e.dataTransfer.getData('text/pb-node'), to: props.node.id}); }"
     @contextmenu.prevent.stop="$parent.$emit('node_menu', props.node.id)">
  <q-icon size="15px" class="vault-node-icon"
          :name="props.node.icon || (props.node.dir ? 'folder' : (props.node.note ? 'description' : 'attach_file'))" />
  <span class="vault-node-label">{{ props.node.label }}</span>
  <q-icon v-if="props.node.lock" name="lock" size="11px" class="vault-node-lock" />
  <span v-if="props.node.pending" class="vault-pending-dot" :title="__TIP__"></span>
  <q-btn v-if="!String(props.node.id).startsWith('::')" flat dense round size="xs" icon="more_horiz"
         class="vault-node-more" @click.stop="$parent.$emit('node_menu', props.node.id)" />
</div>
"""


@ui.page("/brain")
def brain_page():
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    username: str = ng_app.storage.user.get("paperless_user", "")
    ui.add_head_html(_PAGE_CSS)

    # ── State ────────────────────────────────────────────────────────────────
    # `base` is the *whole file* as of the last successful read or write — the
    # merge base, and the yardstick for "is this dirty".
    #
    # The editor holds the body only; the frontmatter source lives in `fm_raw`
    # and is edited through the properties panel. Splitting them means the two
    # halves must be recomposed for every save and every dirty check, which is
    # what `_compose()` is for — nothing else may build the file text.
    st: dict = {
        "rel": "", "base": "", "sha": "", "sig": None,
        "fm_raw": None, "open_fence": "---\n", "close_fence": "---\n",
        "dirty": False, "saving": False, "conflict": False,
        "last_edit": 0.0, "ticks": 0, "attachment": "", "shared": None,
        # The note the user unlocked (managed folders open read-only) — this
        # note only, until another one is opened. Never stored.
        "unlocked": "",
        "props_edit": False,  # properties are read-only until "Edit properties"
        "query": "",  # content search in the tree pane
    }
    # The voice button only exists when dictation is configured, and only means
    # anything with a note open — `_layout_mode` owns its visibility, like the
    # editor's, rather than a binding that polls.
    voice_ui: dict = {"btn": None}
    drawer: dict = {"open": False}
    # Below the CSS breakpoint the tree is a drawer, and a tap must not open a
    # note (the drawer would close over the toolbar the tap was aiming for).
    # Above it there is no drawer, so a tap can open straight away.
    viewport: dict = {"mobile": False}
    pending: set[str] = set()
    # Facts other users shared (`common: true`), keyed by tree id. They are
    # never files in this vault — the owner keeps the only copy — so they are
    # shown from the index, read-only, and no save path can reach them.
    shared: dict[str, dict] = {}

    def _is_shared(rel: str) -> bool:
        return rel == SHARED_ROOT or rel.startswith(SHARED_ROOT + "/")

    def _parent_of(rel: str) -> str:
        parent = Path(rel).parent.as_posix()
        return "" if parent == "." else parent

    def _target_folder() -> str:
        """Where a new note/folder goes from the toolbar: the folder of the open
        note (a node's own menu offers "New note here" for any folder)."""
        return _parent_of(st["rel"]) if st["rel"] else ""

    # ── Small dialogs ────────────────────────────────────────────────────────
    async def _prompt(title: str, label: str, value: str = "") -> str | None:
        with ui.dialog() as dlg, ui.card().style(
            "background:var(--c-surface); min-width:300px; max-width:92vw;"
        ):
            ui.label(title).classes("text-sm font-semibold").style("color:var(--c-text)")
            inp = ui.input(label, value=value).props("outlined dense autofocus").classes("w-full")
            inp.on("keydown.enter", lambda: dlg.submit(inp.value))
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(None)).props("flat dense")
                ui.button(_("OK"), on_click=lambda: dlg.submit(inp.value)).props(
                    "unelevated dense color=primary"
                )
        result = await dlg
        dlg.delete()
        return result

    async def _confirm(text: str) -> bool:
        with ui.dialog() as dlg, ui.card().style(
            "background:var(--c-surface); min-width:280px; max-width:92vw;"
        ):
            ui.label(text).classes("text-sm").style("color:var(--c-text-2)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(False)).props("flat dense")
                ui.button(_("Delete"), on_click=lambda: dlg.submit(True)).props(
                    "unelevated dense color=red"
                )
        answer = await dlg
        dlg.delete()
        return bool(answer)

    # ── Status line ──────────────────────────────────────────────────────────
    def _status(kind: str, extra: str = "") -> None:
        labels = {
            "saved": _("Saved"),
            "saving": _("Saving …"),
            "dirty": _("Unsaved changes"),
            "merged": _("Merged changes from disk"),
            "reloaded": _("Reloaded — changed on disk"),
            "conflict": _("Changed on disk"),
            "reformatted": _("Properties were reformatted"),
            "": "",
        }
        status_label.text = extra or labels.get(kind, "")
        warn = kind in ("conflict", "reformatted")
        status_label.style(f"color:{'var(--c-warn)' if warn else 'var(--c-text-muted)'}")

    # ── Tree ─────────────────────────────────────────────────────────────────
    async def _refresh_tree(keep_pending: bool = False) -> None:
        nonlocal pending
        if not keep_pending:
            try:
                pending = await notes.pending_rel_paths(username)
            except Exception:
                pending = set()
        nodes = await notes.list_tree(username, pending)
        for n in nodes:  # the folders PaperlessBrain manages carry a lock
            if n.get("dir") and managed.managed_root(n["id"]):
                n["lock"] = managed.is_locked(n["id"] + "/x.md")
                n["root"] = True
        shared_node = await _shared_node()
        if shared_node:
            nodes.append(shared_node)
        tree.props["nodes"] = nodes
        tree.update()
        _pending_dot.refresh()
        if "paths" in st:  # after the first load: names may have changed
            asyncio.create_task(_refresh_context())

    async def _shared_node() -> dict | None:
        """The "Shared by others" subtree, one folder per owner."""
        from services.clients import brain

        shared.clear()
        entries = await brain.get_shared_by_others(username)
        if not entries:
            return None
        by_owner: dict[str, list[dict]] = {}
        for entry in entries:
            node_id = f"{SHARED_ROOT}/{entry['user']}/{entry['id']}"
            shared[node_id] = entry
            stem = Path(entry["path"]).stem if entry["path"] else entry["text"][:60]
            by_owner.setdefault(entry["user"], []).append(
                {"id": node_id, "label": stem, "note": True}
            )
        return {
            "id": SHARED_ROOT,
            "label": _("Shared by others"),
            "dir": True,
            "icon": "folder_shared",
            "children": [
                {
                    "id": f"{SHARED_ROOT}/{owner}",
                    "label": owner,
                    "dir": True,
                    "icon": "person",
                    "children": sorted(leaves, key=lambda n: n["label"].lower()),
                }
                for owner, leaves in by_owner.items()
            ],
        }

    def _display_rel(rel: str) -> str:
        """A tree id as the user should read it — shared ids are internal."""
        if not _is_shared(rel):
            return rel
        entry = shared.get(rel)
        if entry:
            return f"{entry['user']} / {Path(entry['path']).stem}"
        tail = rel[len(SHARED_ROOT):].strip("/")
        return tail or _("Shared by others")

    @ui.refreshable
    def _pending_dot() -> None:
        if st["rel"] and st["rel"] in pending:
            ui.icon("circle", size="8px").style("color:var(--c-warn)").tooltip(
                _("Not indexed yet")
            )

    # ── Opening notes ────────────────────────────────────────────────────────
    def _compose(body: str | None = None) -> str:
        """The whole file: frontmatter source + body. The only place the two
        halves are put back together."""
        return join_note(NoteText(
            st["fm_raw"],
            editor.value if body is None else body,
            st["open_fence"],
            st["close_fence"],
        ))

    def _adopt(snap: notes.NoteSnapshot) -> None:
        st.update(rel=snap.rel, base=snap.text, sha=snap.sha, sig=snap.sig, dirty=False)

    def _set_editor(text: str) -> None:
        """Load a whole file: frontmatter into state, body into the editor.

        The editor deliberately never shows the `---` block — it is edited in
        the properties panel, and a second editable copy of the same bytes is
        how the two get out of step. Dirty is derived from `base`, so pushing
        text here can never leave the note looking modified.
        """
        note = split_note(text)
        st["fm_raw"] = note.fm_raw
        st["open_fence"] = note.open_fence
        st["close_fence"] = note.close_fence
        editor.set_value(note.body)
        _props_panel.refresh()
        _props_summary()

    async def _open(rel: str) -> None:
        if not await _flush():
            tree.select(st["rel"] or None)
            return
        st["conflict"] = False
        st["shared"] = None
        _banner.refresh()
        if _is_shared(rel):
            # Read-only: `rel` stays empty, so autosave, the disk poll and the
            # properties panel all have nothing to act on.
            st.update(rel="", attachment="", base="", sha="", sig=None, dirty=False)
            st["shared"] = shared.get(rel)
            _layout_mode.refresh()
            _pending_dot.refresh()
            _set_drawer(False)
            return
        if not notes.is_note(rel):
            st.update(rel="", attachment=rel, base="", sha="", sig=None, dirty=False)
            _layout_mode.refresh()
            _pending_dot.refresh()
            return
        try:
            snap = await notes.read_note(username, rel)
        except notes.NoteConflict:
            ui.notify(_("This note no longer exists."), type="warning")
            await _refresh_tree()
            return
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        st["attachment"] = ""
        if st["unlocked"] != rel:
            st["unlocked"] = ""  # an unlock holds for one note only
        st["props_edit"] = False
        _adopt(snap)
        st["ticks"] = 0
        _layout_mode.refresh()
        _set_editor(snap.text)
        editor.set_context(folder=_parent_of(rel))
        _apply_lock()
        _status("saved")
        _pending_dot.refresh()
        _set_drawer(False)
        tree.props["selected"] = rel
        tree.update()
        _remember(rel)
        st["backlinks"] = []
        _backlinks_view.refresh()
        asyncio.create_task(_load_backlinks())

    def _on_select(e) -> None:
        """One tap opens a note or folds a folder. The actions (rename, move,
        delete, new here) live in each entry's own menu — ⋯, right-click or a
        long press — so nothing needs a "selected but not open" state."""
        rel = e.value or ""
        if not rel:
            # A tap on the already-selected node: QTree reports "unselect".
            rel = st["rel"] or ""
            tree.props["selected"] = rel or None
            tree.update()
            if viewport["mobile"] and rel:
                _set_drawer(False)
            return
        node = _find_node(tree.props["nodes"], rel)
        if node and node.get("dir"):
            expanded = list(tree.props.get("expanded") or [])
            expanded.remove(rel) if rel in expanded else expanded.append(rel)
            tree.props["expanded"] = expanded
            tree.props["selected"] = st["rel"] or None
            tree.update()
            return
        tree.props["selected"] = rel
        tree.update()
        asyncio.create_task(_open(rel))

    def _on_expand(e) -> None:
        # Keep the server-side view of expansion in step with arrow clicks, so
        # a later programmatic toggle does not fight the user.
        tree.props["expanded"] = list(e.value or [])

    def _find_node(nodes: list[dict], rel: str) -> dict | None:
        for node in nodes:
            if node["id"] == rel:
                return node
            found = _find_node(node.get("children", []), rel)
            if found:
                return found
        return None

    # ── Saving ───────────────────────────────────────────────────────────────
    async def _save(force: bool = False) -> bool:
        if not st["rel"] or st["saving"]:
            return True
        text = _compose()
        if not force and text == st["base"]:
            st["dirty"] = False
            return True
        st["saving"] = True
        _status("saving")
        try:
            snap = await notes.save_note(
                username, st["rel"], text,
                expected_sha=None if force else st["sha"],
                base_text=st["base"],
            )
        except notes.NoteConflict as exc:
            st["saving"] = False
            _enter_conflict(exc)
            return False
        except (OSError, notes.VaultPathError) as exc:
            st["saving"] = False
            _status("", _("Could not save: {err}").format(err=exc))
            return False
        st["saving"] = False
        _adopt(snap)
        if snap.merged:
            _set_editor(snap.text)
            _status("merged")
        else:
            _status("saved")
        pending.add(st["rel"])
        _mark_pending_node(st["rel"])
        _pending_dot.refresh()
        return True

    async def _flush() -> bool:
        """Persist pending edits before leaving the note. False = blocked."""
        if not st["rel"] or not st["dirty"] or st["conflict"]:
            return not st["conflict"]
        return await _save()

    def _mark_pending_node(rel: str) -> None:
        node = _find_node(tree.props["nodes"], rel)
        if node and not node.get("pending"):
            node["pending"] = True
            tree.update()

    # ── Conflict handling ────────────────────────────────────────────────────
    def _enter_conflict(exc: notes.NoteConflict) -> None:
        st["conflict"] = True
        st["conflict_kind"] = exc.kind
        st["disk_text"] = exc.disk_text or ""
        st["disk_sha"] = exc.disk_sha or ""
        _status("conflict")
        _banner.refresh()

    async def _resolve_reload() -> None:
        st["conflict"] = False
        try:
            snap = await notes.read_note(username, st["rel"])
        except notes.NoteConflict:
            _banner.refresh()
            return
        _adopt(snap)
        _set_editor(snap.text)
        _status("reloaded")
        _banner.refresh()

    async def _resolve_overwrite() -> None:
        st["conflict"] = False
        _banner.refresh()
        await _save(force=True)

    async def _resolve_copy() -> None:
        stem = Path(st["rel"]).stem
        stamp = datetime.now().strftime("%Y-%m-%d %H%M")
        folder = _parent_of(st["rel"])
        name = f"{stem} (conflict {stamp})"
        try:
            rel = await notes.create_note(username, folder, name)
            await notes.save_note(username, rel, _compose(), expected_sha=None)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        st["conflict"] = False
        _banner.refresh()
        await _refresh_tree()
        await _open(rel)

    async def _follow_move() -> None:
        note_id = properties(split_note(st["base"]).fm_raw).get(ID_KEY, "")
        target = await notes.find_by_pbrain_id(username, str(note_id)) if note_id else None
        if not target:
            ui.notify(_("The note could not be found anywhere in the vault."), type="warning")
            return
        st["conflict"] = False
        st["sha"] = ""  # the file we knew is gone; adopt the new one wholesale
        _banner.refresh()
        await _refresh_tree()
        await _open(target)

    async def _save_as_new() -> None:
        name = await _prompt(_("Save as new note"), _("Name"), Path(st["rel"]).stem)
        if not name:
            return
        try:
            rel = await notes.create_note(username, _parent_of(st["rel"]), name)
            await notes.save_note(username, rel, _compose(), expected_sha=None)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        st["conflict"] = False
        _banner.refresh()
        await _refresh_tree()
        await _open(rel)

    @ui.refreshable
    def _banner() -> None:
        if not st.get("conflict"):
            return
        with ui.column().classes("vault-conflict w-full gap-2"):
            if st.get("conflict_kind") == "deleted":
                ui.label(_("This note no longer exists on disk.")).classes(
                    "text-sm"
                ).style("color:var(--c-text)")
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button(_("Find it"), on_click=_follow_move).props("flat dense")
                    ui.button(_("Save as new note"), on_click=_save_as_new).props(
                        "unelevated dense color=primary"
                    )
                return
            ui.label(_("This note changed on disk while you were editing.")).classes(
                "text-sm"
            ).style("color:var(--c-text)")
            diff = "\n".join(difflib.unified_diff(
                st.get("disk_text", "").splitlines(),
                _compose().splitlines(),
                fromfile=_("On disk"), tofile=_("Yours"), lineterm="", n=2,
            ))
            if diff:
                ui.html(f"<div class='vault-diff'>{_escape(diff)}</div>").classes("w-full")
            with ui.row().classes("gap-2 flex-wrap"):
                ui.button(_("Reload from disk"), on_click=_resolve_reload).props("flat dense")
                ui.button(_("Save as copy"), on_click=_resolve_copy).props("flat dense")
                ui.button(_("Overwrite"), on_click=_resolve_overwrite).props(
                    "unelevated dense color=primary"
                )

    def _escape(text: str) -> str:
        return (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    # ── The one timer: debounce + external-change poll ───────────────────────
    async def _poll_disk() -> None:
        from vault.note_text import try_merge

        rel = st["rel"]
        sig = await asyncio.to_thread(notes.stat_sig, username, rel)
        if sig == st["sig"]:
            return
        try:
            snap = await notes.read_note(username, rel)
        except notes.NoteConflict:
            _enter_conflict(notes.NoteConflict("deleted"))
            return
        except (OSError, notes.VaultPathError):
            return
        st["sig"] = snap.sig
        if snap.sha == st["sha"]:
            return
        if not st["dirty"]:
            _adopt(snap)
            _set_editor(snap.text)
            _status("reloaded")
            return
        merged = try_merge(st["base"], _compose(), snap.text)
        if merged is None:
            _enter_conflict(notes.NoteConflict("modified", snap.text, snap.sha))
            return
        try:
            out = await notes.save_note(
                username, rel, merged, expected_sha=snap.sha, base_text=st["base"]
            )
        except (notes.NoteConflict, OSError, notes.VaultPathError):
            return  # next tick tries again
        _adopt(out)
        _set_editor(out.text)
        _status("merged")

    async def _tick() -> None:
        if not st["rel"] or st["saving"] or st["conflict"]:
            return
        now = time.monotonic()
        if st["dirty"] and now - st["last_edit"] >= AUTOSAVE_IDLE_S:
            await _save()
            return
        st["ticks"] += 1
        if st["ticks"] % POLL_EVERY_TICKS == 0:
            await _poll_disk()

    # ── Frontmatter editing ──────────────────────────────────────────────────
    def _apply_props(set_: dict | None = None, remove: tuple[str, ...] = ()) -> None:
        faithful = blocks_are_faithful(st["fm_raw"])
        st["fm_raw"] = apply_edits(st["fm_raw"], set_=set_, remove=remove)
        _props_panel.refresh()
        st["dirty"] = _compose() != st["base"]
        st["last_edit"] = time.monotonic()
        _status("reformatted" if not faithful else ("dirty" if st["dirty"] else "saved"))

    def _parse_scalar(raw: str) -> object:
        """Interpret a typed value the way YAML would, so a date stays a date
        and `Q3 2026` stays a string."""
        try:
            value = yaml.safe_load(raw)
        except Exception:
            return raw
        return raw if value is None and raw.strip() else value

    async def _add_property() -> None:
        key = await _prompt(_("Add property"), _("Name"))
        if not key:
            return
        key = key.strip()
        current = properties(st["fm_raw"])
        if not key or key in current:
            ui.notify(_("That property already exists."), type="warning")
            return
        if key == ID_KEY:
            ui.notify(_("{key} is assigned automatically.").format(key=ID_KEY), type="warning")
            return
        _apply_props(set_={key: ""})

    async def _edit_yaml() -> None:
        # pbrain_id is the note's identity in the index: change it and the old
        # entry is orphaned while the note re-embeds as a stranger, and two
        # notes carrying the same id collide on one Chroma entry. So it is
        # hidden from the YAML the user edits and put back verbatim after —
        # the escape hatch is for the other properties, not for identity.
        note_id = str(properties(st["fm_raw"]).get(ID_KEY, "") or "")
        visible = apply_edits(st["fm_raw"], remove=(ID_KEY,)) or ""
        with ui.dialog() as dlg, ui.card().style(
            "background:var(--c-surface); width:560px; max-width:94vw;"
        ):
            ui.label(_("Edit properties as YAML")).classes("text-sm font-semibold").style(
                "color:var(--c-text)"
            )
            area = ui.codemirror(
                visible, language="YAML", theme=_cm_theme, line_wrapping=True,
            ).classes("w-full").style("height:260px; border:1px solid var(--c-border); border-radius:6px")
            if note_id:
                ui.label(
                    _("{key} is managed by PaperlessBrain and stays unchanged.").format(
                        key=ID_KEY
                    )
                ).classes("text-xs").style("color:var(--c-text-muted)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(None)).props("flat dense")
                ui.button(_("Apply"), on_click=lambda: dlg.submit(area.value)).props(
                    "unelevated dense color=primary"
                )
        raw = await dlg
        dlg.delete()
        if raw is None:
            return
        try:
            parsed = yaml.safe_load(raw)
        except Exception as exc:
            ui.notify(_("Invalid YAML: {err}").format(err=exc), type="negative")
            return
        if parsed is not None and not isinstance(parsed, dict):
            ui.notify(_("Properties must be a mapping of keys to values."), type="negative")
            return
        # Strip any pbrain_id the user typed in, then restore the real one on top.
        edited = apply_edits(raw if (raw or "").strip() else None, remove=(ID_KEY,)) or ""
        if note_id:
            st["fm_raw"] = f"{ID_KEY}: {note_id}\n{edited}"
        else:
            st["fm_raw"] = edited or None
        _props_panel.refresh()
        st["dirty"] = _compose() != st["base"]
        st["last_edit"] = time.monotonic()
        _status("dirty" if st["dirty"] else "saved")

    @ui.refreshable
    def _props_panel() -> None:
        if not st["rel"]:
            return
        props = properties(st["fm_raw"])
        blocks = raw_blocks(st["fm_raw"])
        in_brain = is_brain_path(Path(st["rel"]), settings.brain_subfolder)
        if not st["props_edit"]:
            _props_read(props, blocks, in_brain)
            return

        with ui.column().classes("w-full gap-1 vault-props"):
            # pbrain_id — identity, assigned by the indexer, never here.
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.label(ID_KEY).classes("text-xs w-32 shrink-0").style("color:var(--c-text-muted)")
                note_id = str(props.get(ID_KEY, "") or "")
                if note_id:
                    ui.label(note_id).classes("vault-id flex-1 w-full")

                    async def _copy_id(value: str = note_id) -> None:
                        ui.clipboard.write(value)
                        ui.notify(_("Copied"))

                    ui.button(icon="content_copy", on_click=_copy_id).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn")
                else:
                    ui.label(_("Assigned when indexed")).classes("text-xs flex-1 w-full").style(
                        "color:var(--c-text-muted); font-style:italic"
                    )

            if not in_brain:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label("dont_ingest").classes("text-xs w-32 shrink-0").style(
                        "color:var(--c-text-muted)"
                    )
                    with ui.column().classes("gap-0 flex-1 min-w-0"):
                        ui.switch(
                            value=bool(props.get("dont_ingest", False)),
                            on_change=lambda e: _apply_props(set_={"dont_ingest": bool(e.value)}),
                        ).props("dense color=primary")
                        ui.label(_("Excluded from the search index.")).classes("text-xs").style(
                            "color:var(--c-text-muted)"
                        )

            if in_brain:
                # Sharing is a brain-fact feature (the chat's memory search
                # reads `common` from the brain index), so it gets a permanent
                # switch here instead of waiting to be typed in by hand.
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label("common").classes("text-xs w-32 shrink-0").style(
                        "color:var(--c-text-muted)"
                    )
                    with ui.column().classes("gap-0 flex-1 min-w-0"):
                        ui.switch(
                            value=bool(props.get("common", False)),
                            on_change=lambda e: _apply_props(set_={"common": bool(e.value)}),
                        ).props("dense color=primary")
                        ui.label(
                            _("Shared with every user: their chat can use it, and it is "
                              "listed under Shared by others. The file stays in your vault.")
                        ).classes("text-xs").style("color:var(--c-text-muted)")

            for key, value in props.items():
                if key == ID_KEY or (key == "dont_ingest" and not in_brain):
                    continue
                if key == "common" and in_brain:
                    continue
                _prop_row(key, value, _raw_scalar(blocks.get(key, ""), key))
                if key == "common":
                    ui.label(
                        _("Has no effect here — only facts in {folder} can be shared.").format(
                            folder=settings.brain_subfolder
                        )
                    ).classes("text-xs w-full").style("color:var(--c-text-muted); padding-left:8.5rem")

            with ui.row().classes("w-full items-center gap-2 pt-1"):
                ui.button(_("Add property"), icon="add", on_click=_add_property).props(
                    "flat dense size=sm"
                )
                ui.button(_("Edit YAML"), on_click=_edit_yaml).props("flat dense size=sm")
                ui.space()
                ui.button(_("Done"), icon="check", on_click=lambda: _props_mode(False)).props(
                    "flat dense size=sm no-caps")

    def _props_mode(edit: bool) -> None:
        st["props_edit"] = edit
        _props_panel.refresh()
        _props_summary()

    def _props_summary() -> None:
        """The collapsed panel says what is in it, not just "Properties"."""
        props = properties(st["fm_raw"]) if st["rel"] else {}
        bits = []
        for k, v in props.items():
            if k == ID_KEY:
                continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "✓" if v else "–"
            bits.append(f"{k}: {v}")
        props_box.props(f'caption="{_esc_attr(" · ".join(bits)[:120])}"')

    def _esc_attr(text: str) -> str:
        return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

    def _props_read(props: dict, blocks: dict, in_brain: bool) -> None:
        """Properties as values, not fields: nothing here changes a file by a
        stray tap. "Edit properties" switches to the fields."""
        with ui.column().classes("w-full gap-1 vault-props"):
            shown = 0
            for key, value in props.items():
                if key == ID_KEY:
                    continue
                shown += 1
                with ui.row().classes("w-full items-start gap-2 no-wrap"):
                    ui.label(key).classes("text-xs w-32 shrink-0").style("color:var(--c-text-muted); padding-top:2px")
                    if key == "common" and in_brain:
                        ui.label(_("Shared with everyone") if value else _("Only you")).classes("text-sm")
                    elif isinstance(value, list):
                        with ui.row().classes("gap-1 flex-wrap flex-1 min-w-0"):
                            for v in value:
                                chip = ui.label(str(v)).classes("vault-chip")
                                if key == "tags":
                                    chip.on("click", lambda _e, t=str(v): _set_query(f"tag:{t}"))
                                    chip.tooltip(_("Notes with this tag"))
                    elif isinstance(value, bool):
                        ui.icon("check" if value else "remove", size="xs").style("color:var(--c-text-2)")
                    else:
                        raw = _raw_scalar(blocks.get(key, ""), key) or ("" if value is None else str(value))
                        ui.label(raw).classes("text-sm flex-1 min-w-0 w-full").style(
                            "color:var(--c-text-2); overflow-wrap:anywhere")
            if not shown:
                ui.label(_("No properties.")).classes("text-xs").style("color:var(--c-text-muted)")
            note_id = str(props.get(ID_KEY, "") or "")
            with ui.row().classes("w-full items-center gap-2 pt-1 no-wrap"):
                if note_id:
                    ui.label(_("Identity")).classes("text-xs w-32 shrink-0").style("color:var(--c-text-muted)")
                    ui.label(note_id).classes("vault-id flex-1 w-full")
                else:
                    ui.space()
                if not _locked():
                    ui.button(_("Edit properties"), icon="edit", on_click=lambda: _props_mode(True)).props(
                        "flat dense size=sm no-caps")

    def _raw_scalar(block: str, key: str) -> str:
        """The value exactly as it stands in the file, for single-line keys.

        Showing `str(parsed)` instead would put `2026-08-10 13:14:47` in the
        field for a source that reads `2026-08-10T13:14:47` — and the moment the
        user touched any other property, that reformatting would be written
        back. The raw text keeps the field honest.
        """
        lines = [ln for ln in block.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if len(lines) != 1:
            return ""
        line = lines[0]
        prefix = f"{key}:"
        return line[len(prefix):].strip() if line.startswith(prefix) else ""

    def _prop_row(key: str, value, raw: str = "") -> None:
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.label(key).classes("text-xs w-32 shrink-0").style("color:var(--c-text-muted)")
            if key in LIST_KEYS or isinstance(value, list):
                current = [str(v) for v in (value or [])] if isinstance(value, list) else []

                def _on_tags(e, k=key):
                    clean = sanitize_tags(list(e.value or []))
                    _apply_props(set_={k: clean})

                ui.select(
                    options=current, value=current, multiple=True,
                    new_value_mode="add-unique", on_change=_on_tags,
                ).props("outlined dense use-chips").classes("flex-1 min-w-0")
            elif key in BOOL_KEYS or isinstance(value, bool):
                ui.switch(
                    value=bool(value),
                    on_change=lambda e, k=key: _apply_props(set_={k: bool(e.value)}),
                ).props("dense color=primary")
            else:
                text = raw or ("" if value is None else str(value))
                field = ui.input(value=text).props("outlined dense").classes("flex-1 min-w-0")

                def _commit(k=key, f=field):
                    _apply_props(set_={k: _parse_scalar(f.value)})

                field.on("blur", lambda k=key, f=field: _commit(k, f))
                field.on("keydown.enter", lambda k=key, f=field: _commit(k, f))
            ui.button(
                icon="close", on_click=lambda k=key: _apply_props(remove=(k,))
            ).props("flat dense round size=sm").classes("card-action-btn")

    # ── Locks: managed folders open read-only (vault/managed.py) ─────────────
    def _locked() -> bool:
        return bool(st["rel"]) and managed.is_locked(st["rel"]) and st["unlocked"] != st["rel"]

    def _apply_lock() -> None:
        editor.set_readonly(_locked())
        _lock_row.refresh()
        _props_panel.refresh()
        _format_bar.refresh()

    async def _unlock() -> None:
        kind = managed.managed_root(st["rel"]) or ""
        why = {
            "memory": _("This is the chat's memory. Changes take effect in the next chat."),
            "findings": _("Findings and dossiers are managed on the Findings page. Editing the file can "
                          "change their state."),
            "experts": _("Experts are edited on the Experts page. Editing the file directly can break "
                         "the expert."),
            "skills": _("Skills are edited on the Experts page. Editing the file directly can break "
                        "the skill."),
            "research": _("A report written by a deep-research run. A rerun of that run rewrites it."),
        }.get(kind, "")
        other = {"findings": ("/findings", _("Open Findings")),
                 "experts": ("/experts", _("Open Experts")),
                 "skills": ("/experts?view=skills", _("Open Experts"))}.get(kind)
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:420px; max-width:92vw;"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("lock_open", size="sm").style("color:var(--c-warn)")
                ui.label(_("Edit a managed note?")).classes("text-base font-semibold").style("color:var(--c-text)")
            ui.label(why).classes("text-sm").style("color:var(--c-text-2)")
            ui.label(_("The note stays unlocked until you open another one.")).classes("text-xs").style(
                "color:var(--c-text-muted)")
            with ui.row().classes("w-full justify-end gap-2 flex-wrap"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(None)).props("flat dense no-caps")
                if other:
                    ui.button(other[1], on_click=lambda: dlg.submit("go")).props("flat dense no-caps")
                ui.button(_("Edit the file"), on_click=lambda: dlg.submit("edit")).props(
                    "unelevated dense no-caps color=primary")
        answer = await dlg
        dlg.delete()
        if answer == "go" and other:
            ui.navigate.to(other[0])
        elif answer == "edit":
            st["unlocked"] = st["rel"]
            _apply_lock()
            editor.focus()

    def _lock_again() -> None:
        st["unlocked"] = ""
        st["props_edit"] = False
        _apply_lock()

    @ui.refreshable
    def _lock_row() -> None:
        if not st["rel"] or not managed.is_locked(st["rel"]):
            return
        with ui.row().classes("vault-lock w-full items-center gap-2 no-wrap"):
            if _locked():
                ui.icon("lock", size="xs")
                ui.label(_("Managed by PaperlessBrain · read-only")).classes("flex-1 min-w-0 text-xs w-full")
                # On the right, away from where a first tap into the text lands —
                # and always through a dialog that says what the folder is for.
                ui.button(_("Unlock to edit"), icon="lock_open", on_click=_unlock).props(
                    "flat dense no-caps size=sm")
            else:
                ui.icon("lock_open", size="xs").style("color:var(--c-warn)")
                ui.label(_("Unlocked — you are editing a managed note")).classes(
                    "flex-1 min-w-0 text-xs w-full").style("color:var(--c-warn)")
                ui.button(_("Lock again"), icon="lock", on_click=_lock_again).props("flat dense no-caps size=sm")

    # ── Tree actions (one per node, from its ⋯ menu, a right-click or a long press) ──
    def _in_folder(title: str, folder: str) -> str:
        return (
            _("{action} in {folder}").format(action=title, folder=folder)
            if folder else _("{action} in the vault root").format(action=title)
        )

    async def _confirm_managed(rel: str) -> bool:
        """Creating, renaming, moving or deleting inside a locked folder asks first."""
        if not managed.is_locked(rel):
            return True
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); max-width:92vw; width:380px;"):
            ui.label(_("This is inside a folder PaperlessBrain manages ({folder}). Change it anyway?").format(
                folder=rel.split("/", 1)[0])).classes("text-sm").style("color:var(--c-text-2)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(False)).props("flat dense no-caps")
                ui.button(_("Change it"), on_click=lambda: dlg.submit(True)).props(
                    "unelevated dense no-caps color=primary")
        ok = bool(await dlg)
        dlg.delete()
        return ok

    async def _new_note(folder: str | None = None) -> None:
        folder = _target_folder() if folder is None else folder
        name = await _prompt(_in_folder(_("New note"), folder), _("Name"))
        if not name:
            return
        if folder and not await _confirm_managed(folder + "/x.md"):
            return
        try:
            rel = await notes.create_note(username, folder, name)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _refresh_tree()
        if folder and folder not in (tree.props.get("expanded") or []):
            tree.props["expanded"] = [*(tree.props.get("expanded") or []), folder]
        await _open(rel)
        editor.focus()

    async def _new_folder(folder: str | None = None) -> None:
        folder = _target_folder() if folder is None else folder
        name = await _prompt(_in_folder(_("New folder"), folder), _("Name"))
        if not name:
            return
        if folder and not await _confirm_managed(folder + "/x.md"):
            return
        try:
            await notes.create_folder(username, folder, name)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _refresh_tree()

    def _protected(rel: str) -> bool:
        if managed.is_root(rel):
            ui.notify(_("{folder} is managed by PaperlessBrain — it cannot be renamed, moved or deleted.").format(
                folder=rel), type="warning")
            return True
        if _is_shared(rel):
            ui.notify(_("Shared notes are read-only."), type="warning")
            return True
        return False

    async def _after_move(old_paths: list[str], old_rel: str, new_rel: str) -> None:
        """Links follow a renamed or moved note (vault/links.py)."""
        moved = vlinks.moved_map(old_paths, old_rel, new_rel)
        if st["rel"] in moved:
            st["rel"] = moved[st["rel"]]  # same bytes, new path — no reload
        elif st["rel"] == old_rel:
            st["rel"] = new_rel
        if st["unlocked"] in moved:
            st["unlocked"] = moved[st["unlocked"]]
        n = await vlinks.rewrite_after_move(username, moved, old_paths) if moved else 0
        await _refresh_tree()
        if st["rel"]:
            await _poll_disk()  # the open note's own links may have changed
        _title.refresh()
        if n:
            ui.notify(_("Updated links in {n} notes.").format(n=n))  # TODO i18n-plural

    async def _rename(rel: str | None = None) -> None:
        rel = rel or st["rel"]
        if not rel or _protected(rel):
            return
        is_dir = not notes.is_note(rel) and _find_node(tree.props["nodes"], rel) is not None and bool(
            (_find_node(tree.props["nodes"], rel) or {}).get("dir"))
        stem = Path(rel).name if is_dir else Path(rel).stem
        name = await _prompt(_("Rename"), _("Name"), stem)
        if not name or name == stem:
            return
        if not await _confirm_managed(rel):
            return
        if not await _flush():
            return
        old_paths = await asyncio.to_thread(vscan.note_paths, username)
        try:
            new_rel = await notes.rename(username, rel, name)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _after_move(old_paths, rel, new_rel)

    async def _move(rel: str, target: str | None = None) -> None:
        if not rel or _protected(rel):
            return
        if target is None:
            target = await _pick_folder(rel)
            if target is None:
                return
        if target == _parent_of(rel):
            return
        if not await _confirm_managed(rel) or (target and not await _confirm_managed(target + "/x.md")):
            return
        if not await _flush():
            return
        old_paths = await asyncio.to_thread(vscan.note_paths, username)
        try:
            new_rel = await notes.move(username, rel, target)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _after_move(old_paths, rel, new_rel)

    async def _pick_folder(rel: str) -> str | None:
        folders: list[str] = []

        def walk(nodes: list[dict]) -> None:
            for n in nodes:
                if n.get("dir") and not _is_shared(n["id"]):
                    folders.append(n["id"])
                    walk(n.get("children", []))

        walk(tree.props["nodes"])
        options = {"": _("Vault root")} | {f: f for f in folders if f != rel and not f.startswith(rel + "/")}
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:420px; max-width:92vw;"):
            ui.label(_("Move “{name}” to …").format(name=Path(rel).name)).classes("text-sm font-semibold").style(
                "color:var(--c-text)")
            pick = ui.select(options, value=_parent_of(rel), with_input=True).props(
                "outlined dense options-dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(None)).props("flat dense no-caps")
                ui.button(_("Move"), on_click=lambda: dlg.submit(pick.value if pick.value is not None else "")).props(
                    "unelevated dense no-caps color=primary")
        out = await dlg
        dlg.delete()
        return out

    async def _delete(rel: str | None = None) -> None:
        rel = rel or st["rel"]
        if not rel or _protected(rel):
            return
        if not await _confirm(_("Delete “{name}” permanently?").format(name=Path(rel).name)):
            return
        if not await _confirm_managed(rel):
            return
        if st["rel"] == rel or st["rel"].startswith(rel + "/"):
            st.update(rel="", base="", sha="", sig=None, dirty=False, conflict=False)
            _layout_mode.refresh()
            _banner.refresh()
        try:
            await notes.delete(username, rel, recursive=True)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _refresh_tree()

    async def _node_menu(rel: str) -> None:
        """The actions of one tree entry, as a sheet — the same on a phone (long
        press, ⋯) and a desktop (right-click, ⋯)."""
        node = _find_node(tree.props["nodes"], rel) or {}
        if _is_shared(rel):
            return
        is_dir = bool(node.get("dir"))
        root = managed.is_root(rel)
        with ui.dialog() as dlg, ui.card().classes("gap-1").style(
                "background:var(--c-surface); width:320px; max-width:92vw; padding:8px 4px;"):
            ui.label(Path(rel).name).classes("text-sm font-semibold px-3 pb-1 w-full").style(
                "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap")
            if root:
                ui.label(_("Managed by PaperlessBrain — cannot be renamed, moved or deleted.")).classes(
                    "text-xs px-3 pb-1").style("color:var(--c-text-muted)")

            def item(icon: str, label: str, action: str, enabled: bool = True) -> None:
                b = ui.button(label, icon=icon, on_click=lambda a=action: dlg.submit(a)).props(
                    "flat no-caps align=left").classes("w-full").style("justify-content:flex-start")
                if not enabled:
                    b.props("disable")

            if is_dir:
                item("note_add", _("New note here"), "note")
                item("create_new_folder", _("New folder here"), "folder")
            elif notes.is_note(rel):
                item("open_in_new", _("Open"), "open")
            item("drive_file_rename_outline", _("Rename"), "rename", not root)
            item("drive_file_move", _("Move to …"), "move", not root)
            item("delete_outline", _("Delete"), "delete", not root)
        action = await dlg
        dlg.delete()
        if action == "note":
            await _new_note(rel)
        elif action == "folder":
            await _new_folder(rel)
        elif action == "open":
            await _open(rel)
        elif action == "rename":
            await _rename(rel)
        elif action == "move":
            await _move(rel)
        elif action == "delete":
            await _delete(rel)

    async def _on_drop(e) -> None:
        a = e.args or {}
        src, dst = str(a.get("from") or ""), str(a.get("to") or "")
        if not src or src == dst or _is_shared(dst):
            return
        node = _find_node(tree.props["nodes"], dst) if dst else {"dir": True}
        target = dst if (node or {}).get("dir") else _parent_of(dst)
        if target == src or target.startswith(src + "/"):
            return
        await _move(src, target)

    async def _index_now() -> None:
        from vault.sync import sync_user

        if not await _flush():
            return
        index_btn.props(add="loading")
        try:
            await sync_user(username, force=True)
        except Exception as exc:
            ui.notify(_("Indexing failed: {err}").format(err=exc), type="negative")
        finally:
            index_btn.props(remove="loading")
        await _refresh_tree()
        if st["rel"]:
            # Sync may have written pbrain_id/dont_ingest into the open note.
            await _poll_disk()
        ui.notify(_("Vault indexed."))

    def _set_drawer(open_: bool) -> None:
        drawer["open"] = open_
        tree_pane.classes(add="open") if open_ else tree_pane.classes(remove="open")
        scrim.classes(add="open") if open_ else scrim.classes(remove="open")

    def _toggle_drawer() -> None:
        _set_drawer(not drawer["open"])

    async def _apply_voice_edit(text: str) -> None:
        """Accept a dictated revision.

        Goes through the editor, not through `notes.save_note`: the dirty check,
        the merge base and the autosave all hang off `editor.value`, and a write
        that bypassed them would be a second save path — the one thing this page
        must not have. The save itself is the ordinary one, kicked off here
        rather than left to the idle timer because an accepted revision is a
        decision, not a keystroke.
        """
        if not st["rel"] or _locked():
            return
        editor.set_value(text)
        # Awaited, not spawned: a save can hit a conflict and render the banner,
        # and a detached task has no client slot to render it into.
        await _save()

    def _open_voice_edit() -> None:
        if not st["rel"]:
            ui.notify(_("Select a note first."), type="warning")
            return
        if _locked():
            ui.notify(_("Unlock the note first."), type="warning")
            return
        _voice_edit()

    def _set_mode(mode: str) -> None:
        st["mode"] = mode
        set_pref("vault_editor_mode", mode)
        editor.set_mode(mode)
        editor_box.classes(add="is-source") if mode == "source" else editor_box.classes(remove="is-source")

    # ── Links, tags, search ──────────────────────────────────────────────────
    async def _refresh_context() -> None:
        """What the editor needs to draw links and complete names."""
        paths = await asyncio.to_thread(vscan.note_paths, username)
        files = await asyncio.to_thread(vscan.other_files, username)
        tags = await asyncio.to_thread(vsearch.all_tags, username)
        st["paths"] = paths
        editor.set_context(notes=paths, tags=tags, files=files, folder=_parent_of(st["rel"]) if st["rel"] else "")

    async def _on_link(link: dict) -> None:
        if link.get("tag") is not None:
            _set_query(f"tag:{link['tag']}")
            if viewport["mobile"]:
                _set_drawer(True)
            return
        href = str(link.get("href") or "")
        if href:
            if "://" in href or href.startswith("mailto:"):
                ui.navigate.to(href, new_tab=True)
                return
            if href.startswith("#") and href[1:].isdigit():
                ui.navigate.to(f"/chat?q=%23{href[1:]}")
                return
            target = vlinks.resolve(vlinks._md_target(st["rel"], href), st.get("paths") or [])
        else:
            raw = str(link.get("wiki") or "")
            name, _sep, heading = raw.partition("#")
            if not name.strip():
                return  # [[#heading]] — this note
            target = vlinks.resolve(name, st.get("paths") or [])
            if target is None:
                await _create_from_link(name)
                return
        if target:
            await _open(target)

    async def _create_from_link(name: str) -> None:
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); max-width:92vw; width:360px;"):
            ui.label(_("“{name}” does not exist yet. Create it?").format(name=name)).classes("text-sm").style(
                "color:var(--c-text-2)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(False)).props("flat dense no-caps")
                ui.button(_("Create note"), on_click=lambda: dlg.submit(True)).props(
                    "unelevated dense no-caps color=primary")
        ok = await dlg
        dlg.delete()
        if not ok:
            return
        folder, _sep, stem = name.strip().replace("\\", "/").rpartition("/")
        folder = folder or _parent_of(st["rel"])
        try:
            rel = await notes.create_note(username, folder, stem)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _refresh_tree()
        await _open(rel)

    def _set_query(q: str) -> None:
        search.set_value(q)

    async def _run_search(q: str) -> None:
        st["query"] = q.strip()
        searching = len(st["query"]) >= 2
        tree_box.set_visibility(not searching)
        results_box.set_visibility(searching)
        if not searching:
            return
        my = st["search_seq"] = st.get("search_seq", 0) + 1
        hits = await asyncio.to_thread(vsearch.search, username, st["query"])
        if my == st["search_seq"]:
            st["hits"] = hits
            _results.refresh()

    @ui.refreshable
    def _results() -> None:
        hits = st.get("hits") or []
        words = vsearch.parse(st["query"]).words
        ui.label(_("{n} notes").format(n=len(hits))).classes("text-xs px-3 pt-1").style(  # TODO i18n-plural
            "color:var(--c-text-muted)")
        for h in hits:
            first = h.lines[0][0] if h.lines else 1
            row = ui.element("div").classes("vault-hit")
            row.on("click", lambda _e, r=h.rel, n=first: _open_at(r, n, words[0] if words else ""))
            with row:
                ui.label(Path(h.rel).stem).classes("t")
                folder = _parent_of(h.rel)
                if folder:
                    ui.label(folder).classes("p")
                for n, line in h.lines:
                    with ui.element("div").classes("s").on(
                            "click.stop", lambda _e, r=h.rel, n=n: _open_at(r, n, words[0] if words else "")):
                        ui.html(_highlight(line, words), sanitize=False)
        if not hits:
            ui.label(_("No note contains that.")).classes("text-xs px-3 py-2").style("color:var(--c-text-muted)")

    def _highlight(line: str, words: list[str]) -> str:
        """The line with its matches marked. Folding can change the length
        (ß → ss); then the line is shown unmarked rather than mis-marked."""
        import html as _html

        folded = vsearch.fold(line)
        if len(folded) != len(line):
            return _html.escape(line)
        spans: list[tuple[int, int]] = []
        for w in words:
            start = 0
            while w and (i := folded.find(w, start)) >= 0:
                spans.append((i, i + len(w)))
                start = i + len(w)
        out, pos = [], 0
        for a, b in sorted(spans):
            if a < pos:
                continue
            out.append(_html.escape(line[pos:a]) + "<mark>" + _html.escape(line[a:b]) + "</mark>")
            pos = b
        return "".join(out) + _html.escape(line[pos:])

    async def _open_at(rel: str, line: int, needle: str) -> None:
        await _open(rel)
        if st["rel"] == rel:
            editor.reveal(line, needle)

    # ── Backlinks ────────────────────────────────────────────────────────────
    async def _load_backlinks() -> None:
        rel = st["rel"]
        st["backlinks"] = await asyncio.to_thread(vlinks.backlinks, username, rel) if rel else []
        if rel == st["rel"]:
            _backlinks_view.refresh()

    @ui.refreshable
    def _backlinks_view() -> None:
        items = st.get("backlinks") or []
        if not st["rel"]:
            return
        with ui.expansion(_("Linked mentions ({n})").format(n=len(items)), icon="link").classes(
                "w-full vault-backlinks"):
            if not items:
                ui.label(_("No other note links here.")).classes("text-xs").style("color:var(--c-text-muted)")
            for src, n, line in items:
                row = ui.element("div").classes("vault-hit")
                row.on("click", lambda _e, r=src, n=n: _open_at(r, n, ""))
                with row:
                    ui.label(Path(src).stem).classes("t")
                    ui.label(line).classes("s")

    # ── Quick switcher (Ctrl/Cmd+O) ──────────────────────────────────────────
    def _recent() -> list[str]:
        return [r for r in (get_pref("vault_recent") or []) if isinstance(r, str)]

    def _remember(rel: str) -> None:
        rec = [rel] + [r for r in _recent() if r != rel]
        set_pref("vault_recent", rec[:12])

    async def _quick_open() -> None:
        paths = st.get("paths") or await asyncio.to_thread(vscan.note_paths, username)
        recent = [r for r in _recent() if r in paths]

        def score(q: str, rel: str) -> int | None:
            """Fuzzy: the query's letters in order in the path; name hits first."""
            name, path = vsearch.fold(Path(rel).stem), vsearch.fold(rel)
            if q in name:
                return 0 if name.startswith(q) else 1
            i = 0
            for c in path:
                if i < len(q) and c == q[i]:
                    i += 1
            return 3 if i == len(q) else None

        with ui.dialog().props("position=top") as dlg, ui.card().classes("gap-1").style(
                "background:var(--c-surface); width:560px; max-width:94vw; margin-top:8vh;"):
            q = ui.input(placeholder=_("Open a note …")).props("outlined dense autofocus clearable").classes("w-full")
            box = ui.column().classes("w-full gap-0").style("max-height:55vh; overflow-y:auto")
            state = {"rows": []}

            def fill() -> None:
                box.clear()
                text = vsearch.fold((q.value or "").strip())
                if not text:
                    rows = recent + [p for p in paths if p not in recent]
                else:
                    scored = [(s, p) for p in paths if (s := score(text, p)) is not None]
                    rows = [p for _s, p in sorted(scored, key=lambda x: (x[0], len(x[1]), x[1].casefold()))]
                state["rows"] = rows[:40]
                with box:
                    for rel in state["rows"]:
                        row = ui.element("div").classes("vault-hit")
                        row.on("click", lambda _e, r=rel: dlg.submit(r))
                        with row:
                            ui.label(Path(rel).stem).classes("t")
                            if _parent_of(rel):
                                ui.label(_parent_of(rel)).classes("p")

            q.on("update:model-value", lambda _e: fill())
            q.on("keydown.enter", lambda: dlg.submit(state["rows"][0] if state["rows"] else None))
            fill()
        rel = await dlg
        dlg.delete()
        if rel:
            await _open(rel)

    # ── Layout ───────────────────────────────────────────────────────────────
    st["mode"] = get_pref("vault_editor_mode", "live") if get_pref("vault_editor_mode", "live") in (
        "live", "source") else "live"
    _cm_theme = "basicLight" if current_theme() == "light" else "basicDark"

    with ui.column().classes("w-full max-w-full p-2 gap-2").style("min-width:0"):
        with ui.element("div").classes("vault-wrap"):
            scrim = ui.element("div").classes("vault-scrim")
            scrim.on("click", _toggle_drawer)

            tree_pane = ui.element("div").classes("vault-tree-pane")
            with tree_pane:
                with ui.row().classes("items-center gap-1 px-2 pt-2 pb-1 no-wrap w-full vault-root-drop"):
                    ui.icon("folder_open", size="xs").style("color:var(--c-text-muted)")
                    ui.label(_("Vault")).classes("text-sm font-semibold flex-1 w-full").style(
                        "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
                    )
                    ui.button(icon="manage_search", on_click=_quick_open).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("Open a note (Ctrl+O)"))
                    ui.button(icon="note_add", on_click=lambda: _new_note()).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("New note"))
                    ui.button(icon="create_new_folder", on_click=lambda: _new_folder()).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("New folder"))
                search = ui.input(placeholder=_("Search in notes …")).props(
                    "outlined dense clearable debounce=300"
                ).classes("mx-2 mb-1")
                search.on("update:model-value", lambda e: _run_search(e.args if isinstance(e.args, str) else ""))
                with search.add_slot("prepend"):
                    ui.icon("search", size="xs")

                tree_box = ui.scroll_area().classes("flex-1 min-h-0").style("padding:0 4px")
                with tree_box:
                    tree = ui.tree(
                        [], label_key="label", node_key="id",
                        on_select=_on_select, on_expand=_on_expand,
                    )
                    tree.add_slot("default-header", _NODE_TEMPLATE.replace(
                        "__TIP__", f"'{_('Not indexed yet')}'"
                    ))
                    tree.on("node_menu", lambda e: _node_menu(str(e.args or "")))
                    tree.on("node_drop", _on_drop)
                results_box = ui.scroll_area().classes("flex-1 min-h-0 vault-results")
                results_box.set_visibility(False)
                with results_box:
                    _results()
                with ui.row().classes("items-center gap-1 px-2 py-1 no-wrap w-full").style(
                    "border-top:1px solid var(--c-border)"
                ):
                    index_btn = ui.button(
                        _("Index now"), icon="sync", on_click=_index_now
                    ).props("flat dense size=sm").classes("flex-1").tooltip(
                        _("Make edited notes findable by the chat right away. "
                          "Happens on its own with the next chat message.")
                    )

            with ui.element("div").classes("vault-main"):
                with ui.row().classes("items-center gap-2 px-2 py-1 no-wrap w-full").style(
                    "border-bottom:1px solid var(--c-border); flex-shrink:0"
                ):
                    ui.button(icon="menu", on_click=_toggle_drawer).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn lg:hidden")

                    @ui.refreshable
                    def _title() -> None:
                        """Breadcrumb; the note's name renames it on a click."""
                        rel = st["rel"] or st.get("attachment") or ""
                        with ui.row().classes("vault-crumbs items-center gap-1 no-wrap flex-1 min-w-0"):
                            if not rel:
                                entry = st.get("shared")
                                if entry:
                                    ui.label(f"{entry['user']} / {Path(entry['path']).stem if entry['path'] else ''}") \
                                        .classes("vault-crumb-name")
                                return
                            parts = rel.split("/")
                            for p in parts[:-1]:
                                ui.label(p).classes("vault-crumb")
                                ui.label("›").classes("vault-crumb-sep")
                            name = ui.label(Path(parts[-1]).stem if st["rel"] else parts[-1]).classes(
                                "vault-crumb-name")
                            if st["rel"] and not managed.is_root(st["rel"]):
                                name.on("click", lambda: _rename(st["rel"]))
                                name.tooltip(_("Rename"))

                    _title()
                    _pending_dot()
                    status_label = ui.label("").classes("text-xs shrink-0").style(
                        "color:var(--c-text-muted)"
                    )
                    if note_voice_enabled():
                        voice_ui["btn"] = ui.button(
                            icon="mic", on_click=_open_voice_edit
                        ).props("flat dense round size=sm").classes(
                            "card-action-btn"
                        ).tooltip(_("Edit note by voice"))
                        voice_ui["btn"].set_visibility(False)
                    note_menu_btn = ui.button(icon="more_vert").props("flat dense round size=sm").classes(
                        "card-action-btn")
                    with note_menu_btn, ui.menu().props("auto-close"):
                        ui.menu_item(_("Live preview"), on_click=lambda: _set_mode("live"))
                        ui.menu_item(_("Source mode"), on_click=lambda: _set_mode("source"))
                        ui.separator()
                        ui.menu_item(_("Rename"), on_click=lambda: _rename(st["rel"]))
                        ui.menu_item(_("Move to …"), on_click=lambda: _move(st["rel"]))
                        ui.menu_item(_("Delete"), on_click=lambda: _delete(st["rel"]))
                        ui.separator()
                        ui.menu_item(_("Open a note … (Ctrl+O)"), on_click=_quick_open)

                _lock_row()
                _banner()

                @ui.refreshable
                def _format_bar() -> None:
                    """A phone has no Ctrl+B: the formatting a note needs, above the keyboard's reach."""
                    if not st["rel"] or _locked():
                        return
                    with ui.row().classes("vault-format w-full items-center gap-0 no-wrap"):
                        for icon, kind, tip in (("title", "heading", _("Heading")), ("format_bold", "bold", _("Bold")),
                                                ("format_italic", "italic", _("Italic")),
                                                ("format_list_bulleted", "list", _("List")),
                                                ("check_box", "task", _("Checkbox")),
                                                ("link", "link", _("Link to a note")),
                                                ("undo", "undo", _("Undo")), ("redo", "redo", _("Redo"))):
                            ui.button(icon=icon, on_click=lambda k=kind: editor.format(k)).props(
                                "flat dense size=sm").classes("card-action-btn").tooltip(tip)

                _format_bar()

                # Collapsed by default: the note is what the user came for,
                # and the properties are mostly machine-managed.
                props_box = ui.expansion(
                    _("Properties"), icon="list_alt", value=False,
                ).classes("w-full").style(
                    "border-bottom:1px solid var(--c-border); flex-shrink:0"
                )
                with props_box:
                    _props_panel()

                editor_box = ui.element("div").classes(
                    "vault-editor w-full" + (" is-source" if st["mode"] == "source" else "")).style(
                    "display:flex; flex-direction:column"
                )
                with editor_box:
                    editor = VaultEditor(
                        "", mode=st["mode"], on_change=lambda v: _on_edit(v),
                        on_save=lambda: asyncio.create_task(_save()),
                        on_link=lambda link: _on_link(link),
                    ).classes("w-full").style("flex:1; min-height:0")
                    _backlinks_view()

                @ui.refreshable
                def _layout_mode() -> None:
                    """Empty state / attachment / shared fact. The editor is
                    built once, above; this only decides what is on screen."""
                    if voice_ui["btn"] is not None:
                        voice_ui["btn"].set_visibility(bool(st["rel"]))
                    note_menu_btn.set_visibility(bool(st["rel"]))
                    _title.refresh()
                    if st["rel"]:
                        editor_box.set_visibility(True)
                        props_box.set_visibility(True)
                        return
                    editor_box.set_visibility(False)
                    props_box.set_visibility(False)
                    entry = st.get("shared")
                    if entry:
                        with ui.column().classes("w-full flex-1 min-h-0 gap-0").style("overflow:auto"):
                            with ui.row().classes("items-center gap-2 px-3 pt-2 no-wrap w-full"):
                                ui.icon("lock", size="xs").style("color:var(--c-text-muted)")
                                ui.label(
                                    _("Shared by {user} — read-only. Only they can change it.").format(
                                        user=entry["user"]
                                    )
                                ).classes("text-xs w-full").style("color:var(--c-text-muted)")
                            if entry.get("due"):
                                ui.label(_("Due: {date}").format(date=entry["due"])).classes(
                                    "text-xs px-3 pt-1"
                                ).style("color:var(--c-text-2)")
                            ui.markdown(
                                escape_intraword_underscores(entry["text"]),
                                extras=[
                                    "fenced-code-blocks", "tables", "cuddled-lists",
                                    "break-on-newline", "task_list",
                                ],
                            ).classes("note-md w-full p-3")
                        return
                    if st["attachment"]:
                        with ui.column().classes("items-center justify-center flex-1 gap-2 p-6 w-full"):
                            ui.icon("attach_file", size="32px").style("color:var(--c-text-muted)")
                            ui.label(_("Attachments are read-only.")).classes("text-sm").style(
                                "color:var(--c-text-2)"
                            )
                            ui.button(
                                _("Open"), icon="open_in_new",
                                on_click=lambda: ui.navigate.to(
                                    f"{FILE_PATH}?path={_quote(st['attachment'])}", new_tab=True
                                ),
                            ).props("flat dense")
                    else:
                        with ui.column().classes("items-center justify-center flex-1 gap-2 p-6 w-full"):
                            ui.icon("note_alt", size="32px").style("color:var(--c-text-muted)")
                            ui.label(_("Select a note to edit it.")).classes("text-sm").style(
                                "color:var(--c-text-muted)"
                            )
                            with ui.row().classes("gap-2"):
                                ui.button(
                                    _("Show notes"), icon="menu", on_click=lambda: _set_drawer(True),
                                ).props("flat dense no-caps").classes("lg:hidden")
                                ui.button(_("Open a note …"), icon="manage_search", on_click=_quick_open).props(
                                    "flat dense no-caps")

                _layout_mode()

    # Built after the editor, because the dialog reads and writes it. One per
    # page, reused for every note — the recorder script is installed with it.
    _voice_edit = (
        create_note_voice_dialog(
            get_note=lambda: editor.value,
            apply_note=_apply_voice_edit,
        )
        if note_voice_enabled()
        else (lambda: None)
    )

    def _on_edit(value: str) -> None:
        if not st["rel"]:
            return
        # Compose with the incoming value rather than editor.value: this also
        # runs for server-side assignments, where the two can differ by one
        # round trip.
        st["dirty"] = _compose(value) != st["base"]
        if st["dirty"]:
            st["last_edit"] = time.monotonic()
            if not st["conflict"]:
                _status("dirty")
        elif not st["conflict"]:
            _status("saved")

    def _quote(rel: str) -> str:
        from urllib.parse import quote

        return quote(rel)

    async def _init() -> None:
        await _refresh_tree()
        _layout_mode.refresh()
        await _refresh_context()
        # Ctrl/Cmd+O opens the quick switcher, as in Obsidian.
        await ui.run_javascript("""
            if (!window.__vaultKeys) {
                window.__vaultKeys = true;
                document.addEventListener('keydown', (e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o') {
                        e.preventDefault(); emitEvent('vault_quick');
                    }
                });
            }
        """)
        try:
            width = await ui.run_javascript("window.innerWidth", timeout=3.0)
        except Exception:
            return   # keep the desktop default rather than guessing
        viewport["mobile"] = int(width) < MOBILE_BP
        # Rotating a tablet crosses the breakpoint, and the tap behaviour has
        # to cross with it.
        await ui.run_javascript("""
            if (!window.__vaultResizeHooked) {
                window.__vaultResizeHooked = true;
                let t;
                window.addEventListener('resize', () => {
                    clearTimeout(t);
                    t = setTimeout(() => emitEvent('vault_resize', window.innerWidth), 250);
                });
            }
        """)
        if viewport["mobile"] and not st["rel"]:
            # Landing on an empty canvas with the tree hidden offers the user
            # nothing to act on — open the drawer so the vault is the first
            # thing they see.
            _set_drawer(True)

    def _on_resize(e) -> None:
        was_mobile = viewport["mobile"]
        viewport["mobile"] = int(e.args or 0) < MOBILE_BP
        if was_mobile and not viewport["mobile"]:
            _set_drawer(False)   # the drawer is a permanent pane above the bp

    ui.on("vault_resize", _on_resize)
    ui.on("vault_quick", lambda _e: _quick_open())

    # The first tree scan touches the vault mount and runs git — deliberately
    # after the response, so a slow mount cannot delay the page itself.
    ui.timer(0.1, _init, once=True)
    ui.timer(TICK_S, _tick)

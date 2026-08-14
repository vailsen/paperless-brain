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
from app_ui.theme import DEFAULT_THEME
from app_ui.vault_routes import FILE_PATH
from config.settings import settings
from i18n import get_translator
from vault import notes
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

_PAGE_CSS = """
<style>
.vault-wrap {
  display: flex; align-items: stretch; gap: 0;
  height: calc(100dvh - 118px); min-height: 380px; width: 100%; min-width: 0;
}
.vault-tree-pane {
  width: 268px; flex-shrink: 0; min-width: 0;
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
.vault-node { display: flex; align-items: center; gap: 6px; min-width: 0; width: 100%; }
.vault-node-icon { color: var(--c-text-muted); flex-shrink: 0; }
.vault-node-label {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 0.8125rem; color: var(--c-text-2); min-width: 0;
}
.vault-node-att .vault-node-label { color: var(--c-text-muted); font-style: italic; }
/* Genuine state the user may want to act on: written but not yet indexed. */
.vault-pending-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--c-warn); flex-shrink: 0; margin-left: auto;
}
.vault-tree-pane .q-tree__node-header { padding: 2px 4px; }
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
   meaning purple still carries. `outline` rather than `border` because it is
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

/* Editor */
.vault-editor { flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
.vault-editor .cm-editor { height: 100%; background: var(--c-surface) !important; }
.vault-editor .cm-gutters { background: var(--c-surface) !important; border-right: 1px solid var(--c-border) !important; }
.vault-editor .cm-scroller { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.84rem; }
.vault-editor .cm-activeLine, .vault-editor .cm-activeLineGutter { background: var(--c-surface-2) !important; }
.vault-id { font-family: ui-monospace, monospace; font-size: 0.75rem; color: var(--c-text-muted); overflow-wrap: anywhere; }

/* Rendered preview — same containment rules as the chat bubbles, for the same
   reason: a wide table must scroll inside its box, never widen the page. */
.note-md { color: var(--c-text-2); font-size: 0.9rem; line-height: 1.65; max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
.note-md h1, .note-md h2, .note-md h3 { color: var(--c-text); font-weight: 600; margin: 0.9rem 0 0.4rem 0; }
.note-md h1 { font-size: 1.25rem; } .note-md h2 { font-size: 1.05rem; } .note-md h3 { font-size: 0.95rem; }
.note-md p { margin: 0 0 0.6rem 0; }
.note-md ul, .note-md ol { padding-left: 1.4rem; margin: 0 0 0.6rem 0; }
.note-md li { margin-bottom: 0.2rem; }
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
  .vault-wrap { height: calc(100dvh - 104px); }
  .vault-tree-pane {
    position: fixed; top: var(--q-header-height, 52px); bottom: 0; left: -272px;
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
<div class="vault-node" :class="props.node.note ? '' : (props.node.dir ? '' : 'vault-node-att')">
  <q-icon size="15px" class="vault-node-icon"
          :name="props.node.dir ? 'folder' : (props.node.note ? 'description' : 'attach_file')" />
  <span class="vault-node-label">{{ props.node.label }}</span>
  <span v-if="props.node.pending" class="vault-pending-dot" :title="__TIP__"></span>
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
        "last_edit": 0.0, "ticks": 0, "attachment": "",
    }
    sel: dict = {"rel": "", "dir": True}
    drawer: dict = {"open": False}
    # Below the CSS breakpoint the tree is a drawer, and a tap must not open a
    # note (the drawer would close over the toolbar the tap was aiming for).
    # Above it there is no drawer, so a tap can open straight away.
    viewport: dict = {"mobile": False}
    pending: set[str] = set()

    def _parent_of(rel: str) -> str:
        parent = Path(rel).parent.as_posix()
        return "" if parent == "." else parent

    def _target_folder() -> str:
        """Where a new note/folder goes: the selection if it is a folder, else
        the folder containing it."""
        if not sel["rel"]:
            return ""
        return sel["rel"] if sel["dir"] else _parent_of(sel["rel"])

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
                    "unelevated dense color=purple"
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
        tree.props["nodes"] = await notes.list_tree(username, pending)
        tree.update()
        _pending_dot.refresh()

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
        if preview.visible:
            preview.set_content(note.body)

    async def _open(rel: str) -> None:
        if not await _flush():
            tree.select(st["rel"] or None)
            return
        st["conflict"] = False
        _banner.refresh()
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
        _adopt(snap)
        st["ticks"] = 0
        _layout_mode.refresh()
        _set_editor(snap.text)
        _status("saved")
        _pending_dot.refresh()
        _set_drawer(False)

    def _on_select(e) -> None:
        """First tap selects and outlines; a further tap on it opens.

        The toolbar (new note, new folder, rename, delete) acts on this
        selection, and on a phone the tree lives in a drawer: if a tap opened
        the note the drawer would close with it, so rename and delete could
        only ever be reached for a note chosen on some earlier visit.
        """
        rel = e.value
        if not rel:
            # QTree reports a tap on the already-selected node as "unselect",
            # which makes it the exact signal for "tapped the same thing
            # again" — so that is what opens it. No timing window: the two taps
            # can be a second or a minute apart. The selection is re-asserted
            # so the outline does not blink off under the finger.
            if sel["rel"]:
                tree.props["selected"] = sel["rel"]
                tree.update()
                _open_selected()
            return
        node = _find_node(tree.props["nodes"], rel)
        sel.update(rel=rel, dir=bool(node and node.get("dir")))
        # Write the selection back: QTree is controlled by this prop, so
        # without it Quasar reverts to the server's (empty) value and the
        # outline never appears on the first tap.
        tree.props["selected"] = rel
        tree.update()
        _sel_label.refresh()
        if not viewport["mobile"]:
            # Desktop has both panes on screen at once, so there is nothing to
            # protect: select and show in the same click.
            _open_selected()

    def _open_selected() -> None:
        """Open the selected note / toggle the selected folder."""
        rel = sel["rel"]
        if not rel:
            return
        if sel["dir"]:
            expanded = list(tree.props.get("expanded") or [])
            if rel in expanded:
                expanded.remove(rel)
            else:
                expanded.append(rel)
            tree.props["expanded"] = expanded
            tree.update()
            return
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
                        "unelevated dense color=purple"
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
                    "unelevated dense color=purple"
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
                    "unelevated dense color=purple"
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
                        ).props("dense color=purple")
                        ui.label(_("Excluded from the search index.")).classes("text-xs").style(
                            "color:var(--c-text-muted)"
                        )

            for key, value in props.items():
                if key == ID_KEY or (key == "dont_ingest" and not in_brain):
                    continue
                _prop_row(key, value, _raw_scalar(blocks.get(key, ""), key))

            with ui.row().classes("w-full items-center gap-2 pt-1"):
                ui.button(_("Add property"), icon="add", on_click=_add_property).props(
                    "flat dense size=sm"
                )
                ui.button(_("Edit YAML"), on_click=_edit_yaml).props("flat dense size=sm")

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
                ).props("dense color=purple")
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

    # ── Toolbar actions ──────────────────────────────────────────────────────
    def _in_folder(title: str) -> str:
        """Name the destination in the dialog title — with one tap now meaning
        'select', where the new item lands has to be visible before it lands."""
        folder = _target_folder()
        return (
            _("{action} in {folder}").format(action=title, folder=folder)
            if folder else _("{action} in the vault root").format(action=title)
        )

    async def _new_note() -> None:
        name = await _prompt(_in_folder(_("New note")), _("Name"))
        if not name:
            return
        try:
            rel = await notes.create_note(username, _target_folder(), name)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _refresh_tree()
        tree.select(rel)
        sel.update(rel=rel, dir=False)
        _sel_label.refresh()
        await _open(rel)

    async def _new_folder() -> None:
        name = await _prompt(_in_folder(_("New folder")), _("Name"))
        if not name:
            return
        try:
            await notes.create_folder(username, _target_folder(), name)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        await _refresh_tree()

    async def _rename() -> None:
        if not sel["rel"]:
            ui.notify(_("Select a note or folder first."), type="warning")
            return
        old = Path(sel["rel"]).name
        stem = Path(sel["rel"]).stem if not sel["dir"] else old
        name = await _prompt(_("Rename"), _("Name"), stem)
        if not name:
            return
        if not await _flush():
            return
        try:
            new_rel = await notes.rename(username, sel["rel"], name)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        if st["rel"] == sel["rel"]:
            st["rel"] = new_rel   # same bytes, new path — no reload needed
        sel["rel"] = new_rel
        await _refresh_tree()
        tree.select(new_rel)
        _sel_label.refresh()
        _layout_mode.refresh()

    async def _delete() -> None:
        if not sel["rel"]:
            ui.notify(_("Select a note or folder first."), type="warning")
            return
        target = sel["rel"]
        if not await _confirm(_("Delete “{name}” permanently?").format(name=Path(target).name)):
            return
        if st["rel"] == target or st["rel"].startswith(target + "/"):
            st.update(rel="", base="", sha="", sig=None, dirty=False, conflict=False)
            _layout_mode.refresh()
            _banner.refresh()
        try:
            await notes.delete(username, target, recursive=True)
        except (OSError, notes.VaultPathError) as exc:
            ui.notify(str(exc), type="negative")
            return
        sel.update(rel="", dir=True)
        _sel_label.refresh()
        await _refresh_tree()

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

    def _toggle_preview() -> None:
        _set_preview(not preview.visible)

    def _set_preview(show: bool) -> None:
        preview.set_visibility(show)
        editor.set_visibility(not show)
        preview_btn.props(f'icon={"edit" if show else "visibility"}')
        preview_btn.tooltip(_("Edit") if show else _("Preview"))
        if show:
            preview.set_content(editor.value)

    # ── Layout ───────────────────────────────────────────────────────────────
    _cm_theme = "basicLight" if ng_app.storage.user.get("theme", DEFAULT_THEME) == "light" else "basicDark"

    with ui.column().classes("w-full max-w-full p-2 gap-2").style("min-width:0"):
        with ui.element("div").classes("vault-wrap"):
            scrim = ui.element("div").classes("vault-scrim")
            scrim.on("click", _toggle_drawer)

            tree_pane = ui.element("div").classes("vault-tree-pane")
            with tree_pane:
                with ui.row().classes("items-center gap-1 px-2 pt-2 pb-1 no-wrap w-full"):
                    ui.icon("folder_open", size="xs").style("color:var(--c-text-muted)")
                    ui.label(_("Vault")).classes("text-sm font-semibold flex-1 w-full").style(
                        "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
                    )
                    ui.button(icon="note_add", on_click=_new_note).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("New note"))
                    ui.button(icon="create_new_folder", on_click=_new_folder).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("New folder"))
                    ui.button(icon="drive_file_rename_outline", on_click=_rename).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("Rename"))
                    ui.button(icon="delete_outline", on_click=_delete).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("Delete"))
                search = ui.input(placeholder=_("Filter …")).props(
                    "outlined dense clearable"
                ).classes("mx-2 mb-1")

                @ui.refreshable
                def _sel_label() -> None:
                    ui.label(
                        _("Selected: {name}").format(name=sel["rel"])
                        if sel["rel"]
                        else (
                            _("Tap to select, tap again to open")
                            if viewport["mobile"] else _("Click a note to open it")
                        )
                    ).classes("text-xs w-full px-2 pb-1").style(
                        "color:var(--c-text-muted); overflow:hidden;"
                        "text-overflow:ellipsis; white-space:nowrap"
                    )

                _sel_label()
                with ui.scroll_area().classes("flex-1 min-h-0").style("padding:0 4px"):
                    tree = ui.tree(
                        [], label_key="label", node_key="id",
                        on_select=_on_select, on_expand=_on_expand,
                    )
                    tree.add_slot("default-header", _NODE_TEMPLATE.replace(
                        "__TIP__", f"'{_('Not indexed yet')}'"
                    ))
                    search.bind_value_to(tree, "filter")
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
                    title = ui.label("").classes("text-sm font-semibold flex-1 w-full").style(
                        "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
                    )
                    _pending_dot()
                    status_label = ui.label("").classes("text-xs shrink-0").style(
                        "color:var(--c-text-muted)"
                    )
                    preview_btn = ui.button(icon="edit", on_click=_toggle_preview).props(
                        "flat dense round size=sm"
                    ).classes("card-action-btn").tooltip(_("Edit"))

                _banner()

                # Open by default: the properties are the half of a note the
                # editor no longer shows, so hiding them behind a click would
                # make them invisible rather than tidy.
                props_box = ui.expansion(
                    _("Properties"), icon="list_alt", value=True,
                ).classes("w-full").style(
                    "border-bottom:1px solid var(--c-border); flex-shrink:0"
                )
                with props_box:
                    _props_panel()

                editor_box = ui.element("div").classes("vault-editor w-full").style(
                    "display:flex; flex-direction:column"
                )
                with editor_box:
                    editor = ui.codemirror(
                        "", language="Markdown", theme=_cm_theme,
                        line_wrapping=True, indent="  ",
                        on_change=lambda e: _on_edit(e.value),
                        keymap={"Mod-s": lambda _e: asyncio.create_task(_save())},
                    ).classes("w-full").style("flex:1; min-height:0")
                    preview = ui.markdown(
                        "", extras=["fenced-code-blocks", "tables"]
                    ).classes("note-md w-full p-3").style("flex:1; overflow:auto; min-height:0")
                    # Reading is the common case; the pencil switches to source.
                    editor.set_visibility(False)

                @ui.refreshable
                def _layout_mode() -> None:
                    """Empty state / attachment view. The editor is built once,
                    above; this only decides what is on screen."""
                    if st["rel"]:
                        title.text = st["rel"]
                        editor_box.set_visibility(True)
                        props_box.set_visibility(True)
                        return
                    editor_box.set_visibility(False)
                    props_box.set_visibility(False)
                    if st["attachment"]:
                        title.text = st["attachment"]
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
                        title.text = ""
                        with ui.column().classes("items-center justify-center flex-1 gap-2 p-6 w-full"):
                            ui.icon("note_alt", size="32px").style("color:var(--c-text-muted)")
                            ui.label(_("Select a note to edit it.")).classes("text-sm").style(
                                "color:var(--c-text-muted)"
                            )
                            ui.button(
                                _("Show notes"), icon="menu", on_click=lambda: _set_drawer(True),
                            ).props("flat dense").classes("lg:hidden")

                _layout_mode()

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
        try:
            width = await ui.run_javascript("window.innerWidth", timeout=3.0)
        except Exception:
            return   # keep the desktop default rather than guessing
        viewport["mobile"] = int(width) < MOBILE_BP
        _sel_label.refresh()   # the hint differs per mode, and was built before this
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
        if was_mobile != viewport["mobile"]:
            _sel_label.refresh()

    ui.on("vault_resize", _on_resize)

    # The first tree scan touches the vault mount and runs git — deliberately
    # after the response, so a slow mount cannot delay the page itself.
    ui.timer(0.1, _init, once=True)
    ui.timer(TICK_S, _tick)

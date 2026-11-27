"""The note vault's editor: CodeMirror 6 with Obsidian-style live preview.

`src/index.mjs` is the editor (decorations, checkboxes, links, tables,
formatting, autocomplete), bundled into `dist/` by
`scripts/build_vault_editor.sh` from the pinned `package-lock.json`. The
bundle is committed — no Node at runtime or in the Docker build.

The element holds the *body* of a note; `app_ui/pages/brain.py` keeps the
frontmatter apart and composes the file (`_compose`). Decorations never
change the text, so a note opened and saved without typing keeps its bytes.

Value sync: the browser sends `update` (150 ms after the last keystroke);
the server sets a value with `set_value`, which bumps `rev` — the browser
replaces its text only on a new `rev`, never on an echo of its own.
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui
from nicegui.events import GenericEventArguments


class VaultEditor(ui.element, component="vault_editor.js", esm={"pb-vault-editor": "dist"}):
    def __init__(self, value: str = "", *, mode: str = "live", readonly: bool = False,
                 on_change: Callable[[str], None] | None = None,
                 on_save: Callable[[], object] | None = None,
                 on_link: Callable[[dict], object] | None = None) -> None:
        super().__init__()
        self._value = value
        self._props["value"] = value
        self._props["rev"] = 0
        self._props["mode"] = mode
        self._props["readonly"] = readonly
        self._props["notes"] = []
        self._props["tags"] = []
        self._props["files"] = {}
        self._props["folder"] = ""
        self._on_change = on_change
        self._crlf = "\r\n" in value

        def _update(e: GenericEventArguments) -> None:
            text = e.args if isinstance(e.args, str) else str(e.args or "")
            self._value = self._line_ends(text)
            if self._on_change:
                self._on_change(self._value)

        self.on("update", _update)
        if on_save:
            self.on("save", lambda _e: on_save())
        if on_link:
            self.on("link", lambda e: on_link(e.args or {}))

    @property
    def value(self) -> str:
        return self._value

    def _line_ends(self, text: str) -> str:
        """CodeMirror reads CRLF and gives back LF. A note written with CRLF
        (Windows) gets its CRLF back, or the first edit anywhere in it would
        rewrite every line ending in the file."""
        return text.replace("\r\n", "\n").replace("\n", "\r\n") if self._crlf else text

    def set_value(self, value: str) -> None:
        """Replace the text (a reload, a merge, a dictated revision)."""
        self._crlf = "\r\n" in value
        self._value = value
        self._props["value"] = value
        self._props["rev"] += 1
        self.update()
        if self._on_change:
            self._on_change(value)

    def set_mode(self, mode: str) -> None:
        self._props["mode"] = mode
        self.update()

    def set_readonly(self, readonly: bool) -> None:
        self._props["readonly"] = bool(readonly)
        self.update()

    def set_context(self, *, notes: list[str] | None = None, tags: list[str] | None = None,
                    files: dict[str, str] | None = None, folder: str | None = None) -> None:
        """Note paths for [[ completion and link state, tags for # completion,
        attachments by file name for embeds, the open note's folder."""
        if notes is not None:
            self._props["notes"] = notes
        if tags is not None:
            self._props["tags"] = tags
        if files is not None:
            self._props["files"] = files
        if folder is not None:
            self._props["folder"] = folder
        self.update()

    def format(self, kind: str) -> None:
        self.run_method("format", kind)

    def reveal(self, line: int, needle: str = "") -> None:
        self.run_method("reveal", line, needle)

    def focus(self) -> None:
        self.run_method("focus")

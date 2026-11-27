// The note vault's editor: CodeMirror 6 with Obsidian-style live preview.
//
// The text stays Markdown. Markup (#, **, [[…]], link targets, list and task
// markers) is hidden or drawn as widgets everywhere except on the lines the
// cursor is on; nothing is ever re-serialized, so a note opened and closed
// without typing keeps its exact bytes (docs/ux-overhaul-plan.md, 5.1).
//
// Modes: "live" (default), "source" (plain Markdown), and read-only, which is
// live preview without a cursor (locked notes, shared facts).

import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab, redo, undo } from "@codemirror/commands";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { syntaxTree } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { Compartment, EditorSelection, EditorState, RangeSetBuilder, StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType, keymap } from "@codemirror/view";

const WIKI_RE = /(!?)\[\[([^\]\n|#]*)(#[^\]\n|]*)?(?:\|([^\]\n]*))?\]\]/g;
const TAG_RE = /(^|[\s(])#([\p{L}\p{N}_\-/]*[\p{L}_\-/][\p{L}\p{N}_\-/]*)/gu;

// ── Widgets ────────────────────────────────────────────────────────────────

class CheckboxWidget extends WidgetType {
  constructor(checked, pos, disabled) {
    super();
    this.checked = checked;
    this.pos = pos;
    this.disabled = disabled;
  }
  eq(o) {
    return o.checked === this.checked && o.pos === this.pos && o.disabled === this.disabled;
  }
  toDOM(view) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "pbe-check";
    box.checked = this.checked;
    box.disabled = this.disabled;
    box.addEventListener("mousedown", (e) => e.preventDefault());
    box.addEventListener("click", (e) => {
      e.preventDefault();
      if (this.disabled) return;
      // One character: "[ ]" ↔ "[x]". An ordinary transaction, so it goes
      // through the same dirty → autosave → merge path as typing.
      const at = this.pos + 1;
      const cur = view.state.doc.sliceString(at, at + 1);
      view.dispatch({ changes: { from: at, to: at + 1, insert: cur === " " ? "x" : " " } });
    });
    return box;
  }
  ignoreEvent() {
    return true;
  }
}

class BulletWidget extends WidgetType {
  eq() {
    return true;
  }
  toDOM() {
    const s = document.createElement("span");
    s.className = "pbe-bullet";
    s.textContent = "•";
    return s;
  }
}

class HrWidget extends WidgetType {
  eq() {
    return true;
  }
  toDOM() {
    const d = document.createElement("div");
    d.className = "pbe-hr";
    return d;
  }
}

class ImageWidget extends WidgetType {
  constructor(src, alt) {
    super();
    this.src = src;
    this.alt = alt;
  }
  eq(o) {
    return o.src === this.src;
  }
  toDOM() {
    const img = document.createElement("img");
    img.className = "pbe-img";
    img.src = this.src;
    img.alt = this.alt || "";
    img.loading = "lazy";
    return img;
  }
}

function esc(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function inlineHtml(s) {
  // Cells and similar: escaped text with **bold**, *italic*, `code` only.
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>");
}

class TableWidget extends WidgetType {
  constructor(text, from) {
    super();
    this.text = text;
    this.from = from;
  }
  eq(o) {
    return o.text === this.text;
  }
  toDOM(view) {
    const wrap = document.createElement("div");
    wrap.className = "pbe-table-wrap";
    const rows = this.text.split("\n").filter((l) => l.trim());
    const cells = (l) => {
      let t = l.trim();
      if (t.startsWith("|")) t = t.slice(1);
      if (t.endsWith("|")) t = t.slice(0, -1);
      return t.split("|").map((c) => c.trim());
    };
    let html = "<table class='pbe-table'>";
    rows.forEach((l, i) => {
      if (i === 1 && /^[\s|:\-]+$/.test(l)) return;
      const tag = i === 0 ? "th" : "td";
      html += "<tr>" + cells(l).map((c) => `<${tag}>${inlineHtml(c)}</${tag}>`).join("") + "</tr>";
    });
    wrap.innerHTML = html + "</table>";
    // A click puts the cursor into the table: its source comes back.
    wrap.addEventListener("mousedown", (e) => {
      if (!view.state.facet(EditorView.editable)) return;
      e.preventDefault();
      view.dispatch({ selection: { anchor: this.from } });
      view.focus();
    });
    return wrap;
  }
  ignoreEvent() {
    return true;
  }
}

// ── Which lines show their source ──────────────────────────────────────────

// Only while the editor has focus: a note just opened (cursor at the start)
// or left for the tree reads as rendered, as in Obsidian.
const setFocus = StateEffect.define();
const focusField = StateField.define({
  create: () => false,
  update(v, tr) {
    for (const e of tr.effects) if (e.is(setFocus)) v = e.value;
    return v;
  },
});
const focusTracker = EditorView.focusChangeEffect.of((_state, focusing) => setFocus.of(focusing));

function cursorLines(state) {
  const lines = new Set();
  if (!state.facet(EditorView.editable) || !state.field(focusField, false)) return lines;
  for (const r of state.selection.ranges) {
    const a = state.doc.lineAt(r.from).number;
    const b = state.doc.lineAt(r.to).number;
    for (let n = a; n <= b; n++) lines.add(n);
  }
  return lines;
}

function touchesCursor(state, from, to, lines) {
  const a = state.doc.lineAt(from).number;
  const b = state.doc.lineAt(to).number;
  for (let n = a; n <= b; n++) if (lines.has(n)) return true;
  return false;
}

// ── Inline decorations (a view plugin: visible ranges only) ────────────────

const hide = Decoration.replace({});
const mark = (cls) => Decoration.mark({ class: cls });

function buildInline(view, cfg) {
  const { state } = view;
  const lines = cursorLines(state);
  const deco = [];
  const add = (from, to, d) => {
    if (from < to || d.point) deco.push(d.range(from, to));
  };
  const readOnly = !state.facet(EditorView.editable);
  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from,
      to,
      enter: (node) => {
        const name = node.name;
        const onCursor = touchesCursor(state, node.from, node.to, lines);
        if (/^ATXHeading[1-6]$/.test(name)) {
          const lvl = name.slice(-1);
          const line = state.doc.lineAt(node.from);
          deco.push(Decoration.line({ class: `pbe-h pbe-h${lvl}` }).range(line.from));
          return;
        }
        if (name === "HeaderMark" && !onCursor) {
          const next = state.doc.sliceString(node.to, node.to + 1);
          add(node.from, node.to + (next === " " ? 1 : 0), hide);
          return;
        }
        if (name === "StrongEmphasis") add(node.from, node.to, mark("pbe-strong"));
        if (name === "Emphasis") add(node.from, node.to, mark("pbe-em"));
        if (name === "Strikethrough") add(node.from, node.to, mark("pbe-strike"));
        if (name === "InlineCode") add(node.from, node.to, mark("pbe-code"));
        if ((name === "EmphasisMark" || name === "StrikethroughMark" || name === "CodeMark") && !onCursor) {
          const parent = node.node.parent;
          if (parent && parent.name !== "FencedCode") add(node.from, node.to, hide);
          return;
        }
        if (name === "Link" || name === "Image") {
          const text = state.doc.sliceString(node.from, node.to);
          const m = /^(!?)\[([^\]]*)\]\(([^)\s]*)[^)]*\)$/.exec(text);
          if (!m) return;
          if (name === "Image") {
            if (!onCursor) {
              const src = cfg.resolveSrc(m[3]);
              if (src) add(node.from, node.to, Decoration.replace({ widget: new ImageWidget(src, m[2]) }));
            }
            return false;
          }
          const labelFrom = node.from + 1;
          const labelTo = labelFrom + m[2].length;
          add(labelFrom, labelTo, Decoration.mark({ class: "pbe-link", attributes: { "data-href": m[3] } }));
          if (!onCursor) {
            add(node.from, labelFrom, hide);
            add(labelTo, node.to, hide);
          }
          return false;
        }
        if (name === "URL" && node.node.parent && node.node.parent.name !== "Link") {
          add(node.from, node.to, Decoration.mark({ class: "pbe-link", attributes: { "data-href": state.doc.sliceString(node.from, node.to) } }));
        }
        if (name === "Blockquote") {
          for (let p = node.from; p <= node.to; ) {
            const line = state.doc.lineAt(p);
            deco.push(Decoration.line({ class: "pbe-quote" }).range(line.from));
            p = line.to + 1;
          }
        }
        if (name === "QuoteMark" && !onCursor) {
          const next = state.doc.sliceString(node.to, node.to + 1);
          add(node.from, node.to + (next === " " ? 1 : 0), hide);
          return;
        }
        if (name === "FencedCode") {
          for (let p = node.from; p <= node.to; ) {
            const line = state.doc.lineAt(p);
            deco.push(Decoration.line({ class: "pbe-codeblock" }).range(line.from));
            p = line.to + 1;
          }
          return false;
        }
        if (name === "ListMark") {
          const after = state.doc.sliceString(node.to, node.to + 4);
          const isTask = /^ \[[ xX]\]/.test(after);
          const bullet = /^[-*+]$/.test(state.doc.sliceString(node.from, node.to));
          if (isTask && (!onCursor || readOnly)) add(node.from, node.to + 1, hide);
          else if (bullet && !onCursor) add(node.from, node.to, Decoration.replace({ widget: new BulletWidget() }));
          return;
        }
        if (name === "TaskMarker") {
          const checked = /x/i.test(state.doc.sliceString(node.from, node.to));
          const sel = state.selection.main;
          const inside = state.facet(EditorView.editable) && sel.from >= node.from && sel.from <= node.to;
          if (!inside) add(node.from, node.to, Decoration.replace({ widget: new CheckboxWidget(checked, node.from, readOnly && !cfg.checkInReadOnly) }));
          if (checked) {
            const line = state.doc.lineAt(node.from);
            add(node.to, line.to, mark("pbe-done"));
          }
          return;
        }
      },
    });
    // Wikilinks and tags: not Markdown syntax, found by pattern.
    const text = state.doc.sliceString(from, to);
    for (const m of text.matchAll(WIKI_RE)) {
      const start = from + m.index;
      const end = start + m[0].length;
      if (inCode(state, start)) continue;
      const target = (m[2] || "").trim();
      const onCursor = touchesCursor(state, start, end, lines);
      if (m[1] === "!") {
        const src = cfg.resolveEmbed(target);
        if (src && !onCursor) add(start, end, Decoration.replace({ widget: new ImageWidget(src, target) }));
        continue;
      }
      const ok = cfg.exists(target);
      const label = m[4] !== undefined ? m[4] : (m[2] || "") + (m[3] || "");
      const cls = "pbe-wiki" + (ok ? "" : " pbe-unresolved");
      const attrs = { "data-wiki": target + (m[3] || "") };
      if (onCursor) {
        add(start + 2, end - 2, Decoration.mark({ class: cls, attributes: attrs }));
        continue;
      }
      if (m[4] !== undefined) {
        const labelFrom = end - 2 - m[4].length;
        add(start, labelFrom, hide);
        add(labelFrom, end - 2, Decoration.mark({ class: cls, attributes: attrs }));
        add(end - 2, end, hide);
      } else {
        add(start, start + 2, hide);
        add(start + 2, end - 2, Decoration.mark({ class: cls, attributes: attrs }));
        add(end - 2, end, hide);
      }
      void label;
    }
    for (const m of text.matchAll(TAG_RE)) {
      const start = from + m.index + m[1].length;
      if (inCode(state, start) || state.doc.lineAt(start).text.trimStart().startsWith("#" + " ")) continue;
      const line = state.doc.lineAt(start);
      if (/^#{1,6}\s/.test(line.text)) {
        // "# Heading" is not a tag, "#tag" at the start of a line is.
        if (start === line.from) continue;
      }
      add(start, start + 1 + m[2].length, Decoration.mark({ class: "pbe-tag", attributes: { "data-tag": m[2] } }));
    }
  }
  return Decoration.set(deco, true);
}

function inCode(state, pos) {
  let n = syntaxTree(state).resolveInner(pos, 1);
  for (; n; n = n.parent) {
    if (n.name === "InlineCode" || n.name === "FencedCode" || n.name === "CodeBlock") return true;
  }
  return false;
}

function inlinePlugin(cfg) {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = buildInline(view, cfg);
      }
      update(u) {
        if (u.docChanged || u.viewportChanged || u.selectionSet || u.focusChanged ||
            u.transactions.some((t) => t.effects.some((e) => e.is(setFocus))) ||
            syntaxTree(u.startState) !== syntaxTree(u.state) || u.transactions.some((t) => t.reconfigured)) {
          this.decorations = buildInline(u.view, cfg);
        }
      }
    },
    { decorations: (v) => v.decorations },
  );
}

// ── Block decorations (tables, rules): must come from a state field ────────

function buildBlocks(state) {
  const b = new RangeSetBuilder();
  const lines = cursorLines(state);
  const items = [];
  syntaxTree(state).iterate({
    enter: (node) => {
      if (node.name === "Table") {
        if (!touchesCursor(state, node.from, node.to, lines)) {
          const from = state.doc.lineAt(node.from).from;
          const to = state.doc.lineAt(node.to).to;
          items.push([from, to, Decoration.replace({ widget: new TableWidget(state.doc.sliceString(from, to), from), block: true })]);
        }
        return false;
      }
      if (node.name === "HorizontalRule" && !touchesCursor(state, node.from, node.to, lines)) {
        items.push([node.from, node.to, Decoration.replace({ widget: new HrWidget() })]);
      }
    },
  });
  items.sort((x, y) => x[0] - y[0]);
  for (const [f, t, d] of items) b.add(f, t, d);
  return b.finish();
}

const blockField = StateField.define({
  create: (s) => buildBlocks(s),
  update(v, tr) {
    if (tr.docChanged || tr.selection || tr.reconfigured || tr.effects.some((e) => e.is(setFocus))) {
      return buildBlocks(tr.state);
    }
    return v;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// ── Formatting commands ─────────────────────────────────────────────────────

function wrap(view, before, after = before) {
  const { state } = view;
  view.dispatch(
    state.changeByRange((r) => {
      const text = state.sliceDoc(r.from, r.to);
      if (text.startsWith(before) && text.endsWith(after) && text.length >= before.length + after.length) {
        const inner = text.slice(before.length, text.length - after.length);
        return { changes: { from: r.from, to: r.to, insert: inner }, range: EditorSelection.range(r.from, r.from + inner.length) };
      }
      return {
        changes: [{ from: r.from, insert: before }, { from: r.to, insert: after }],
        range: EditorSelection.range(r.from + before.length, r.to + before.length),
      };
    }),
  );
  view.focus();
  return true;
}

function linePrefix(view, kind) {
  const { state } = view;
  const changes = [];
  const seen = new Set();
  for (const r of state.selection.ranges) {
    for (let p = r.from; p <= r.to; ) {
      const line = state.doc.lineAt(p);
      p = line.to + 1;
      if (seen.has(line.number)) continue;
      seen.add(line.number);
      const t = line.text;
      const indent = /^\s*/.exec(t)[0];
      const rest = t.slice(indent.length);
      let next;
      if (kind === "task") {
        // plain → "- [ ]" → "- [x]" → plain
        if (/^[-*+] \[ \] /.test(rest)) next = rest.replace(/^([-*+]) \[ \] /, "$1 [x] ");
        else if (/^[-*+] \[[xX]\] /.test(rest)) next = rest.replace(/^[-*+] \[[xX]\] /, "");
        else if (/^[-*+] /.test(rest)) next = rest.replace(/^([-*+]) /, "$1 [ ] ");
        else next = "- [ ] " + rest;
      } else if (kind === "list") {
        next = /^[-*+] /.test(rest) ? rest.replace(/^[-*+] (\[[ xX]\] )?/, "") : "- " + rest;
      } else if (kind === "heading") {
        const m = /^(#{1,6}) /.exec(rest);
        const lvl = m ? m[1].length : 0;
        next = (lvl >= 3 ? "" : "#".repeat(lvl + 1) + " ") + rest.replace(/^#{1,6} /, "");
      } else if (kind === "quote") {
        next = /^> ?/.test(rest) ? rest.replace(/^> ?/, "") : "> " + rest;
      }
      if (next !== undefined && next !== rest) changes.push({ from: line.from + indent.length, to: line.to, insert: next });
    }
  }
  if (changes.length) view.dispatch({ changes });
  view.focus();
  return true;
}

export function runFormat(view, kind) {
  switch (kind) {
    case "bold": return wrap(view, "**");
    case "italic": return wrap(view, "*");
    case "strike": return wrap(view, "~~");
    case "code": return wrap(view, "`");
    case "link": return wrap(view, "[[", "]]");
    case "task": case "list": case "heading": case "quote": return linePrefix(view, kind);
    case "undo": return undo(view);
    case "redo": return redo(view);
  }
  return false;
}

// ── Autocomplete: [[note and #tag ───────────────────────────────────────────

function completions(cfg) {
  return (ctx) => {
    const wiki = ctx.matchBefore(/\[\[[^\]\n|#]*$/);
    if (wiki) {
      const q = wiki.text.slice(2).toLowerCase();
      const opts = cfg.notes()
        .filter((n) => !q || n.label.toLowerCase().includes(q) || n.path.toLowerCase().includes(q))
        .slice(0, 50)
        .map((n) => ({
          label: n.label,
          detail: n.dir || undefined,
          apply: (view, c, from, to) => {
            const after = view.state.sliceDoc(to, to + 2);
            const insert = n.link + (after === "]]" ? "" : "]]");
            view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + n.link.length + 2 } });
          },
        }));
      return { from: wiki.from + 2, options: opts, filter: false };
    }
    const tag = ctx.matchBefore(/(^|\s)#[\p{L}\p{N}_\-/]*$/u);
    if (tag && tag.text.trim().length >= 2) {
      const start = tag.from + tag.text.indexOf("#") + 1;
      const q = tag.text.trim().slice(1).toLowerCase();
      return {
        from: start,
        options: cfg.tags().filter((t) => t.toLowerCase().startsWith(q)).slice(0, 30).map((t) => ({ label: t, type: "keyword" })),
      };
    }
    return null;
  };
}

// ── Theme ───────────────────────────────────────────────────────────────────

const theme = EditorView.theme({
  "&": { height: "100%", backgroundColor: "transparent", color: "var(--c-text-2)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "inherit", lineHeight: "1.65", fontSize: "0.92rem" },
  ".cm-content": { padding: "14px 18px 40vh", maxWidth: "820px", caretColor: "var(--c-text)" },
  ".cm-line": { padding: "0" },
  ".cm-cursor": { borderLeftColor: "var(--c-text)" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": { backgroundColor: "var(--c-accent-soft) !important" },
  ".cm-gutters": { display: "none" },
  ".cm-tooltip": { background: "var(--c-surface)", border: "1px solid var(--c-border)", borderRadius: "6px" },
  ".cm-tooltip-autocomplete ul li[aria-selected]": { background: "var(--c-accent-soft)", color: "var(--c-text)" },
});

// ── The editor ──────────────────────────────────────────────────────────────

export function createEditor(parent, opts) {
  const cfg = {
    notes: () => opts.notes || [],
    tags: () => opts.tags || [],
    exists: (t) => (opts.exists ? opts.exists(t) : true),
    resolveSrc: (u) => (opts.resolveSrc ? opts.resolveSrc(u) : (/^https?:/.test(u) ? u : null)),
    resolveEmbed: (t) => (opts.resolveEmbed ? opts.resolveEmbed(t) : null),
    checkInReadOnly: false,
  };
  const modeC = new Compartment();
  const editC = new Compartment();
  const live = () => [inlinePlugin(cfg), blockField];
  let timer = null;

  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: opts.value || "",
      extensions: [
        history(),
        closeBrackets(),
        markdown({ base: markdownLanguage, codeLanguages: languages }),
        EditorView.lineWrapping,
        theme,
        focusField,
        focusTracker,
        autocompletion({ override: [completions(cfg)], icons: false }),
        keymap.of([
          { key: "Mod-s", run: () => (opts.onSave && opts.onSave(), true) },
          { key: "Mod-b", run: (v) => runFormat(v, "bold") },
          { key: "Mod-i", run: (v) => runFormat(v, "italic") },
          { key: "Mod-l", run: (v) => runFormat(v, "task") },
          { key: "Mod-k", run: (v) => runFormat(v, "link") },
          ...closeBracketsKeymap, ...completionKeymap, ...historyKeymap, ...defaultKeymap, indentWithTab,
        ]),
        modeC.of(opts.mode === "source" ? [] : live()),
        editC.of([EditorView.editable.of(!opts.readOnly), EditorState.readOnly.of(!!opts.readOnly)]),
        EditorView.updateListener.of((u) => {
          if (!u.docChanged || !opts.onChange) return;
          clearTimeout(timer);
          timer = setTimeout(() => opts.onChange(view.state.doc.toString()), 150);
        }),
        EditorView.domEventHandlers({
          mousedown: (e) => {
            const el = e.target.closest && e.target.closest(".pbe-wiki, .pbe-link, .pbe-tag");
            if (!el) return false;
            // A link opens on a plain click — to edit it, move the cursor in with
            // the keyboard, or click beside it. Ctrl/Cmd-click edits as usual.
            if (e.ctrlKey || e.metaKey || e.button !== 0) return false;
            // On a line being edited the markup is visible: a click edits it.
            if (view.state.facet(EditorView.editable)) {
              const lines = cursorLines(view.state);
              if (lines.has(view.state.doc.lineAt(view.posAtDOM(el)).number)) return false;
            }
            e.preventDefault();
            if (el.dataset.wiki !== undefined && opts.onLink) opts.onLink({ wiki: el.dataset.wiki });
            else if (el.dataset.href !== undefined && opts.onLink) opts.onLink({ href: el.dataset.href });
            else if (el.dataset.tag !== undefined && opts.onLink) opts.onLink({ tag: el.dataset.tag });
            return true;
          },
        }),
      ],
    }),
  });

  return {
    view,
    getValue: () => view.state.doc.toString(),
    flush: () => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
        opts.onChange && opts.onChange(view.state.doc.toString());
      }
    },
    setValue(text) {
      if (text === view.state.doc.toString()) return;
      clearTimeout(timer);
      const sel = view.state.selection.main;
      const pos = Math.min(sel.head, text.length);
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, selection: { anchor: pos } });
    },
    setMode(mode) {
      view.dispatch({ effects: modeC.reconfigure(mode === "source" ? [] : live()) });
    },
    setReadOnly(ro) {
      view.dispatch({ effects: editC.reconfigure([EditorView.editable.of(!ro), EditorState.readOnly.of(!!ro)]) });
    },
    refresh() {
      // Lists changed (note names, tags): redraw the decorations.
      view.dispatch({ selection: view.state.selection });
    },
    format: (kind) => runFormat(view, kind),
    reveal(line, needle) {
      const n = Math.max(1, Math.min(line || 1, view.state.doc.lines));
      const l = view.state.doc.line(n);
      let from = l.from;
      let to = l.from;
      if (needle) {
        const i = l.text.toLowerCase().indexOf(needle.toLowerCase());
        if (i >= 0) {
          from = l.from + i;
          to = from + needle.length;
        }
      }
      view.dispatch({ selection: { anchor: from, head: to }, effects: EditorView.scrollIntoView(from, { y: "center" }) });
    },
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

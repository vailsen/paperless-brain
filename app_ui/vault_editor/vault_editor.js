// The note vault's editor element (app_ui/vault_editor/__init__.py).
import { createEditor } from "pb-vault-editor";

export default {
  template: `<div class="pbe-root"></div>`,
  props: {
    value: String,
    rev: Number,
    mode: String,
    readonly: Boolean,
    notes: Array,
    tags: Array,
    files: Object,
    folder: String,
  },
  mounted() {
    const self = this;
    this.names = new Map();
    this.indexNotes();
    this.ed = createEditor(this.$el, {
      value: this.value || "",
      mode: this.mode,
      readOnly: this.readonly,
      get notes() { return self.noteOpts; },
      get tags() { return self.tags || []; },
      exists: (t) => self.exists(t),
      resolveSrc: (u) => self.resolveSrc(u),
      resolveEmbed: (t) => self.resolveEmbed(t),
      onChange: (v) => { self.lastSent = v; self.$emit("update", v); },
      onSave: () => { self.ed.flush(); self.$emit("save"); },
      onLink: (l) => self.$emit("link", l),
    });
  },
  beforeUnmount() {
    if (this.ed) this.ed.destroy();
  },
  watch: {
    rev() { this.ed.setValue(this.value || ""); },
    mode(m) { this.ed.setMode(m); },
    readonly(r) { this.ed.setReadOnly(r); },
    notes() { this.indexNotes(); this.ed.refresh(); },
    files() { this.ed.refresh(); },
  },
  methods: {
    indexNotes() {
      // name → [paths] and the completion entries ({label, link, dir}).
      this.names = new Map();
      this.paths = new Set();
      const opts = [];
      for (const p of this.notes || []) {
        const noExt = p.replace(/\.md$/i, "");
        this.paths.add(noExt.toLowerCase());
        const base = noExt.split("/").pop();
        const key = base.toLowerCase();
        if (!this.names.has(key)) this.names.set(key, []);
        this.names.get(key).push(noExt);
      }
      for (const p of this.notes || []) {
        const noExt = p.replace(/\.md$/i, "");
        const base = noExt.split("/").pop();
        const unique = this.names.get(base.toLowerCase()).length === 1;
        const dir = noExt.includes("/") ? noExt.slice(0, noExt.lastIndexOf("/")) : "";
        opts.push({ label: base, path: noExt, dir, link: unique ? base : noExt });
      }
      opts.sort((a, b) => a.label.localeCompare(b.label));
      this.noteOpts = opts;
    },
    exists(t) {
      const k = (t || "").replace(/\.md$/i, "").toLowerCase();
      if (!k) return true; // [[#heading]] — this note
      return this.paths.has(k) || this.names.has(k.split("/").pop());
    },
    fileUrl(rel) {
      return "/api/vault/file?path=" + encodeURIComponent(rel);
    },
    resolveSrc(u) {
      if (/^https?:\/\//.test(u)) return u;
      if (!u || u.startsWith("/") || u.includes("..")) return null;
      const rel = (this.folder ? this.folder + "/" : "") + decodeURIComponent(u);
      return this.fileUrl(rel);
    },
    resolveEmbed(t) {
      const name = (t || "").split("/").pop().toLowerCase();
      const rel = (this.files || {})[name];
      return rel && /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(rel) ? this.fileUrl(rel) : null;
    },
    format(kind) { this.ed.format(kind); },
    reveal(line, needle) { this.ed.reveal(line, needle); },
    focus() { this.ed.focus(); },
    flush() { this.ed.flush(); },
  },
};

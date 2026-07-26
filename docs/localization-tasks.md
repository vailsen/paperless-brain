# Localization — Implementation Tasks

> Working checklist for Claude Code. Execute **one or two phases at a time**,
> `/clear` between phases. The invariants and do-not list live in the
> **Localization (i18n)** section of `CLAUDE.md` — that section is authoritative;
> this file is the step-by-step. Do not start a phase before the previous one
> meets its acceptance criteria.

---

## Phase 0 — Scaffold the mechanism on ONE page

Goal: prove the full extract→translate→compile→switch loop on a single page
before touching the rest of the app.

### Setup
- [ ] Create `babel.cfg` at project root with exactly:
      ```ini
      [python: **.py]
      ```
- [ ] Create `i18n.py` (root or `config/`) with `LOCALES_DIR`, `DEFAULT_LANG = "de"`,
      `SUPPORTED_LANGUAGES = {"de": "Deutsch", "en": "English"}`, and
      `get_translator()` exactly as specified in the CLAUDE.md section.
      Set `LOCALES_DIR` to point at the `locales/` folder relative to `i18n.py`.
- [ ] Create the `locales/` directory.
- [ ] Verify `ui.run(...)` is called with `storage_secret=...`. If missing, add it.
      (Required for `app.storage.user`; without it, the language preference cannot
      be stored.) Confirm other settings already persist there — they should.

### Settings UI
- [ ] Add a `language_setting()` selector (per the CLAUDE.md reference snippet)
      and render it as the **first** element at the top of the user settings page.
- [ ] Confirm `on_change` writes `app.storage.user["language"] = e.value` and
      then calls `ui.navigate.reload()`.

### Wrap one page
- [ ] Pick one representative page (suggest the documents page).
- [ ] Add `_ = get_translator()` as the first line of that page function.
- [ ] Wrap every UI-visible string on that page in `_( )`. Leave everything
      outside this page untouched for now.

### Generate, translate, compile
- [ ] `pybabel extract -F babel.cfg -o locales/messages.pot .`
- [ ] Open `locales/messages.pot` and confirm the wrapped strings appear as
      `msgid` blocks with empty `msgstr`. Confirm header says `charset=UTF-8`
      (not `CHARSET`) so umlauts survive.
- [ ] `pybabel init -i locales/messages.pot -d locales -l en`
- [ ] Translate the handful of strings in `locales/en/LC_MESSAGES/messages.po`.
- [ ] `pybabel compile -d locales`

### Acceptance — all must hold before Phase 1
- [ ] Selecting **English** in settings reloads and flips that one page to English.
- [ ] Selecting **German** shows the original German text (no `.mo` lookup needed).
- [ ] A fresh session with no stored preference defaults to German.
- [ ] No page other than the chosen one is affected.
- [ ] No global `_` exists; the translator is resolved inside the page function.

---

## Phase 1 — Full string sweep

Goal: every human-visible string in the app is wrapped; the app still runs in
German with no regressions.

### Sweep
- [ ] Wrap every UI-visible string in `app_ui` (labels, buttons, menus, tab/dialog
      titles, table headers, tooltips, placeholders, empty-states, `ui.notify`).
- [ ] Wrap user-facing messages emitted from `services/`, `pipelines/`, and
      `werkbank/` that surface in the UI or in Telegram/Hermes replies.
- [ ] Add `_ = get_translator()` at the top of every page that gained wrapped
      strings. For helper/component functions, call `get_translator()` inside or
      pass `_` in — never cache it at module level.

### Guard against the known traps (see CLAUDE.md do-not list)
- [ ] Do **not** wrap log messages, log-only exceptions, internal dict/enum keys,
      config keys, Paperless custom-field names, API identifiers, or frontmatter.
- [ ] Replace any `_(f"...")` with `_("...{n}...").format(n=...)`.
- [ ] Move any **import-time** strings (module-level constants, class attrs,
      default args, label-maps) so the `_()` call happens at render time.
- [ ] Add a `# TODO i18n-plural` comment at every singular/plural site; wrap it as
      a plain string for now.

### Verify
- [ ] `pybabel extract -F babel.cfg -o locales/messages.pot .` collects all strings.
- [ ] Grep the rendered UI paths for stray hardcoded German string literals; none
      should remain in user-visible positions.
- [ ] Run the app in German end-to-end; confirm no behavioral or layout regressions.

### Acceptance
- [ ] All wrapped strings appear in `messages.pot`.
- [ ] No hardcoded German in any rendered UI surface.
- [ ] App runs fully in German via the fallback path, unchanged in behavior.

---

## Phase 2 — Translate and compile

Goal: the whole app switches de↔en cleanly.

- [ ] `pybabel update -i locales/messages.pot -d locales`
      (updates the existing English `.po` with all new strings; do **not** re-`init`).
- [ ] Fill **every** English `msgstr` in `locales/en/LC_MESSAGES/messages.po`:
      - [ ] Natural, professional document-management English (not literal).
      - [ ] Imperative for buttons/actions ("Save", "Upload", "Delete").
      - [ ] Consistent terminology across the whole catalog.
      - [ ] Every `{placeholder}` preserved exactly.
      - [ ] Proper nouns / technical tokens (PaperSage, Paperless-ngx, Obsidian,
            Telegram, ChromaDB) left unchanged.
- [ ] Resolve every `fuzzy` flag pybabel added; remove the flag once verified.
- [ ] `pybabel compile -d locales`
- [ ] Manually switch de↔en in the running app and spot-check several pages,
      including notifications and error states.

### Acceptance
- [ ] Entire app switches de↔en via the settings selector.
- [ ] No empty `msgstr` remains (an empty one renders German inside the English UI).
- [ ] All placeholders intact; proper nouns untouched.
- [ ] No `fuzzy` entries ship unreviewed.

---

## Phase 3 — Plurals & further languages (later, optional)

- [ ] Introduce `ngettext` at each `# TODO i18n-plural` site; add the
      `Plural-Forms` header to the catalogs.
- [ ] Exercise the "Adding a language later" steps once with a third locale
      (add to `SUPPORTED_LANGUAGES` → `pybabel init -l xx` → translate → compile).

### Acceptance
- [ ] Count-dependent strings read correctly for n=1 and n>1 in both languages.
- [ ] A third language can be added with only the four documented steps — no other
      code changes.

---

## Definition of done (whole feature)

- [ ] Language selector sits at the top of user settings; choice persists in
      `app.storage.user`.
- [ ] Every UI-visible string is translatable; German is the fallback.
- [ ] English catalog is complete, compiled, and verified across the app.
- [ ] Adding another language requires only the four documented steps.
- [ ] No global translator; no new persistence layer; no `_(f"...")`; no wrapped
      logs/keys.

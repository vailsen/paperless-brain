# UI polish — round 2 (remaining tasks)

Follow-up to `ui-polish-tasks.md`. Tag normalisation, the `Action required` badge split, the
chip icons, the fade mask, the deadline sort, and the calendar dot markers all shipped and
verified. What follows is what did not land, plus two small things spotted during review.

Same workflow: one phase at a time, `/clear` between phases, verify in the browser.

---

## Shipped and verified — do not revisit

- Tag chips normalised to a fixed luminance band, hue preserved from Paperless.
- Tag overflow capped with `+N`.
- `Action required` moved out of the tag row onto its own line with a bolt icon.
- Suggestion chips: emoji removed, icons added, right-edge fade mask.
- Deadline table sorted nearest-first.
- `220 overdue` renders as an actionable link rather than a static red number.
- Calendar deadline markers changed from filled purple squares to dots.
- Send button changed from a red stop square to a send arrow.

---

## Phase A — Monochrome all icons

Nothing from Phase 3 of the previous doc shipped. Icons still render in purple, blue, and cyan
across every surface, and none of those hues encode anything.

### A.1 Top navigation

- [x] All nav icons inherit the label colour. No per-item hue.
- [x] The active item is the only differentiated one — full-opacity text plus the purple
      underline that already exists. One signal, not three; do not also change the icon colour.
- [x] Verify the active state survives a greyscale screenshot (the underline carries it).

### A.2 Document card actions

- [x] Graph, pin, and download icons render at `--text-muted` at rest.
- [x] Card hover lifts them to `--text-secondary`; individual icon hover to `--text-primary`.
- [x] Consider making the card body itself the open affordance so one icon can be dropped
      entirely. Three icons across eight visible cards is 24 competing targets in the panel.

### A.3 Document detail header

Currently four icons in four different colours: cyan graph, purple pin, blue refresh, red X.

- [x] All four render monochrome at `--text-muted`, lifting on hover.
- [x] **The red X is the priority.** Closing a panel is not destructive — nothing is deleted,
      nothing is lost, the action is fully reversible. Red there spends the strongest available
      signal on the safest action in the view, which inverts the entire point of this pass.
      Reserve red for operations that destroy data.

### A.4 Dashboard card footers

- [x] Sync, info, vault, and theme icons all render at `--text-muted`.
- [x] System status dots stay green. Those are genuine state, not decoration.

### A.5 Reserve purple

- [x] Audit remaining purple usage. Keep: logo, active nav item, user message bubbles, active
      selection, send button.
- [x] Neutralise everything else — document ID links get the standard link treatment, the model
      dropdown border goes to `--border`.

### Acceptance criteria

- A greyscale screenshot of any view loses no information except system status dots and
  `Action required` badges.
- No two icons inside the same container render in different hues.
- No red anywhere except on operations that destroy data.

---

## Phase B — Sentence case on buttons and tabs

The string changes were made but the labels still render uppercase. This is not a content
problem: Quasar applies `text-transform: uppercase` to `q-btn` and `q-tab` by default, so
editing the Python string has no visible effect.

### B.1 Global fix

- [x] Add once at app init rather than per-button:

```python
ui.add_head_html(
    '<style>'
    '.q-btn__content, .q-tab__label { text-transform: none !important; }'
    '</style>'
)
```

- [x] Alternatively, apply `.props('no-caps')` per component if a global override causes
      problems elsewhere. The global version is preferred — per-component is something you will
      forget on the next button you add.

### B.2 Surfaces to verify after the change

- [x] Suggestion chip row: `What can you do?`, `Open deadlines`, `Next month`,
      `New documents`, `New emails`, `Invoices`, `Write a letter`, `Write an email`,
      `Save chat to Paperless`.
- [x] Document detail tabs: `Summary`, `Full text`, `Dates / references`, `Tables`.
- [x] The `Show` action in the hidden-deadlines bar.
- [x] Any other `q-btn` or `q-tab` in the codebase — grep for both.

### B.3 What to leave uppercase

Do **not** change these. Small-caps eyebrow labels with letter-spacing are a legitimate pattern
in dense dashboards, and they are doing useful work here:

- `SYSTEM STATUS`, `DEADLINES`, `CALENDAR & DEADLINES`, `METADATA`, `SUMMARY`, `NOTES`,
  `DEADLINES / ACTIONS`
- Table column headers: `DATE`, `DESCRIPTION`, `DOC`

The distinction: an eyebrow labels a region, a button names an action. Regions can shout
quietly; actions read better in sentence case.

### Acceptance criteria

- No button or tab label renders in all caps.
- Section eyebrows and table headers are unchanged.

---

## Phase C — Two small corrections

### C.1 Green tags still read hot

- [x] The green `Valentin` chip sits visibly brighter than the blue and pink chips at the same
      nominal lightness. This is the symptom of skipping the perceptual correction table in
      `normalize_tag_color`.
- [x] Either apply the `_LIGHTNESS_CORRECTION` lookup from the previous doc, or switch the
      conversion to OKLCH and clamp the `L` channel directly. OKLCH is perceptually uniform by
      construction, so the correction table becomes unnecessary — `coloraide` does it in one
      call. Worth the dependency if you would rather not maintain a hand-tuned table.
- [x] Low priority. Cosmetic, not structural.

### C.2 Progress ring reads as 100%

- [x] The document count ring is a closed circle at 98%, so it communicates "complete" when
      six documents are unindexed.
- [x] Either leave a visible gap proportional to the missing percentage, or drop the ring
      entirely — `461 / 467` already states the exact figure, and the ring adds no information
      the number does not already carry. Dropping it is the simpler fix and removes a decorative
      element from the densest card on the dashboard.

---

## Out of scope

Recorded so it does not get relitigated:

- **Layout.** Unchanged and sound. The document detail split view in particular works well.
- **Suggestion chip count.** Horizontal scroll plus fade mask is an acceptable affordance.
  Revisit only if click instrumentation shows the tail is unused.
- **Accent hue.** Dark plus violet is fine. The problem was colour without meaning.
- **Spacing scale normalisation.** Its own branch, later — do not mix it into a pass that is
  already touching colour in the same templates.

---

## Implementation notes — round 2

### Phase B was the real defect, and it explains the rest

`.q-btn{…text-transform:uppercase}` and `.q-tab{…}` live in Quasar's own stylesheet
(`nicegui/static/quasar.unimportant.prod.css`). Round 1 edited the Python label strings, so
the catalog and the source read sentence case while the rendered UI stayed uppercase. Fixed
globally in `theme.py` per B.1. Verified in the served HTML.

### The premise of Phase A was partly wrong

"Nothing from Phase 3 of the previous doc shipped" does not match the code. Verified against
the running server rather than assumed — the theme CSS is served, and the old
`.nav-btn .q-icon{color:#a855f7}` rule is gone:

- **A.1, A.2, A.4 had already shipped** in round 1 (`desktop-nav-btn`/`nav-active`,
  `card-action-btn`, `footer-icon`). Their rules are present and unshadowed in the served CSS.
- **A.3 partly did not apply.** The `hub` and `push_pin` icons were already on
  `card-action-btn`. The "blue refresh, red X" the phase describes do not exist: both are
  `text-gray-400` in `document_dialog.py` and were before this round. The only red in the
  codebase is on Delete and Discard — which destroy data, so A.3's own rule says keep them.
- **What genuinely remained was A.5**, the wider purple sweep that round 1 explicitly scoped
  out and recorded. That is what this round actually delivered: 19 decorative icon sites
  neutralised across chat, browser, brain, settings, dashboard, cluster dialog and werkbank.

If your build really does show cyan/blue/red icons on the detail header, it is not this
source tree — check you are not on the deployed image, which is still at the last tagged
release.

### What stays coloured, and why

Every remaining coloured icon encodes state, which is exactly what the acceptance criterion
allows: the pinned-card overlay (active selection), the sync-running and no-model-configured
banners, and werkbank's `check_circle`/done markers. Werkbank keeps a deliberate status
vocabulary — purple running, green done, red failed — applied to icon *and* label together.
Neutralising the icon while leaving the label green was tried and reverted: a half-broken
pair reads worse than a consistent colour system.

### C.1 — the correction table was not skipped, it was insufficient

`_LIGHTNESS_CORRECTION` shipped in round 1 exactly as specified, and green still read hot.
Rather than re-tune a table by hand, `normalize_tag_color` now works in OKLCh: hue is taken
from the Paperless hex via OKLab, then re-emitted at a fixed perceptual `L` and `C`. Equal L
in OKLab *is* equal apparent brightness, so the correction table is gone and the failure mode
cannot recur for any hue. Implemented with stdlib `math` — the sRGB→OKLab matrices are about
fifteen lines, so no `coloraide` dependency was added. Tests now assert the invariant
(identical L across eight hues) instead of an 8-point tolerance.

Requires `oklch()` CSS support: Chrome 111+, Safari 15.4+, Firefox 113+. Fine for this app's
deployment, but it is a hard requirement rather than a progressive enhancement — an older
browser gets no chip colour at all.

### C.2 — ring removed

The ring read as closed at 98 % because `stroke-linecap="round"` adds a half-stroke cap at
each end (8 px against a ~5 px gap at 461/467). Dropped entirely rather than gapped, per the
phase's own preference: `461 / 467` and `98 % indexed in ChromaDB` were already rendered as
text directly beside it, so the ring carried no information. `_ring_svg` and the now-unused
`math` import are gone.

### Also done

- Removed the 🧠 emoji from the brain result card. Its neighbouring icon had just been
  neutralised, and a colour glyph beside a monochrome icon reads as a mistake. Replaced with a
  muted corner badge matching the vault card's `MD`.

### Not done

- **Emoji elsewhere in UI chrome** — roughly a dozen sites remain (`⚠`, `✓`, `🗑`, `📂`, `🗜`,
  `📓`). Round 1 scoped emoji removal to the chip row and round 2 does not raise it. Changing
  them rewrites a dozen msgids and needs re-translation, so it wants to be its own phase.
- **Coloured tile backgrounds** on the vault and brain result cards (`#0d2137`, `#1a0a2e`).
  The icons on them are now neutral. These are surfaces, not icons, and no phase has asked
  for them.

### Verification

267 tests green, app boots with no tracebacks, and the Phase B override plus the round-1
Phase 3 rules were confirmed present in the served HTML rather than assumed. The colour maths
is unit-tested. **The visual acceptance criteria remain unverified in a browser** — there are
no session credentials on this machine, so greyscale-screenshot checks and "does the chip row
now read sentence case" still need your eyes.

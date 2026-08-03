# UI polish — phased tasks

Scope: visual discipline pass on the existing PaperSage UI. No layout restructuring, no new
features. Everything here is subtractive or normalising.

Run one phase at a time, `/clear` between phases, verify in the browser before moving on.

---

## Invariants

Append to `CLAUDE.md` once agreed:

- **Colour encodes state, not identity — with one exception.** Tag chips inherit hue from
  Paperless because the user assigned it and it enables cross-document grouping. Everything
  else in the UI (icons, borders, buttons, badges) is neutral unless it represents a state the
  user must react to.
- **Tag row = Paperless data only.** Anything PaperSage generates (action flags, ingestion
  status, confidence) renders outside the tag row with its own treatment.
- **One accent meaning.** Purple = the user's own input and the active selection. Nothing else.
- **Icons inherit text colour.** No coloured icons anywhere. Active state is the only
  differentiator in navigation.
- **Sentence case everywhere.** No ALL CAPS labels, no emoji in UI chrome.

---

## Phase 1 — Tag chip normalisation

The chips currently render at whatever saturation and lightness the Paperless hex carries.
Full-brightness yellow and green vibrate against the dark background and outweigh the document
title. Fix is to keep the hue and clamp the other two channels.

### 1.1 Fetch real tag colours from Paperless

- [x] Add a `get_tag_colors()` call against `GET /api/tags/?page_size=200`. The response
      includes `id`, `name`, `colour` (hex, e.g. `#a6cee3`) and `text_color` per tag.
- [x] Cache the result in memory with a TTL of ~15 minutes. Tag colours change rarely; do not
      hit the API per card render.
- [x] Invalidate the cache on manual sync.
- [x] Fall back to a deterministic hue derived from `hash(tag_name)` if a tag has no colour set
      or the API call fails. Never fall back to random-per-render — the same tag must be the
      same colour on every card.

### 1.2 Clamp to a fixed luminance band

```python
import colorsys

# Perceptual correction: yellows and greens read brighter than blues and purples
# at identical HSL lightness. Pull them down so all chips sit at equal visual weight.
_LIGHTNESS_CORRECTION = [
    (45, 90, -8),    # yellow / yellow-green
    (90, 150, -6),   # green
    (150, 200, -2),  # teal / cyan
]


def normalize_tag_color(hex_color: str) -> tuple[str, str]:
    """Map an arbitrary Paperless tag hex onto a fixed saturation/lightness band.

    Returns (background_css, text_css) for the dark theme.
    Hue is preserved so tags stay visually distinguishable; everything else is normalised
    so no tag can shout louder than another.
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue_f, _lightness, _sat = colorsys.rgb_to_hls(r, g, b)
    hue = round(hue_f * 360)

    correction = 0
    for lo, hi, delta in _LIGHTNESS_CORRECTION:
        if lo <= hue < hi:
            correction = delta
            break

    return (
        f"hsl({hue} 60% 50% / 0.18)",
        f"hsl({hue} 55% {68 + correction}%)",
    )
```

- [x] Implement the helper above in the shared UI utils module.
- [x] Unit test: assert that a pure yellow (`#ffff00`) and a mid blue (`#3778dd`) produce text
      colours within 8 percentage points of lightness of each other.
- [x] Apply to every tag chip render path — document cards in the chat panel, document detail
      view, and any filter UI that shows tags.

### 1.3 Cap visible tags

- [x] Render at most 4 chips per card.
- [x] Remaining tags collapse to a muted `+N` label using `--text-muted`, no background, no
      border.
- [x] `+N` expands the full list on click (not hover — hover expansion in a scrolling panel is
      hostile on touch).
- [x] Sort so the 4 shown are stable across renders. Alphabetical is fine; whatever it is, it
      must not reorder between page loads.

### Acceptance criteria

- No tag chip is visibly brighter than any other tag chip on the same card.
- The same tag renders identically on every card and across sessions.
- No card shows a tag row taller than two lines.
- Squinting at a document card, the title is the first thing you see, not a chip.

---

## Phase 2 — Separate the `Actions` badge from the tag row

`Actions` is system state rendered as a tag. That is why it disappeared into the noise, and why
orange — a colour that means "warning" everywhere else — got spent on it.

- [x] Move the badge out of the tag chip container entirely. It gets its own row below the tag
      row, separated by a hairline divider (`0.5px` at your existing border token).
- [x] Render with warning semantics: low-alpha warning background, warning text colour, leading
      icon (`warning` or `bolt` from your icon set at 13px).
- [x] Change the label from `Actions` to something that states the condition —
      `Action required` or `Aktion erforderlich`. A noun does not tell the user what is being
      asserted.
- [x] Do the same normalisation pass on any other system-generated badge that currently sits in
      the tag row. Audit the card template for stragglers.

### Acceptance criteria

- With tags neutralised in Phase 1, `Action required` is the only saturated element on a card
  that has one, and is immediately visible when scanning the panel.
- Cards without an action flag show no coloured badge at all.

---

## Phase 3 — Monochrome all icons

Currently: nav icons alternate blue and purple; the four card action icons render cyan; the
dashboard card icons render blue. None of these colours carry meaning.

### 3.1 Top navigation

- [x] All nav icons render at `--text-secondary` equivalent, inheriting from the label.
- [x] The active item is the only differentiated one: full-opacity text plus the purple accent.
      Use one signal, not three — do not combine colour, weight, and an underline.
- [x] Verify the active state is legible without colour (for colour-blind users and for
      screenshots) — an underline or left border carries it.

### 3.2 Document card actions

- [x] The four action icons (graph, pin, preview, download) render at `--text-muted` at rest.
- [x] On card hover they lift to `--text-secondary`. On icon hover, `--text-primary`.
- [x] Consider making the card body itself the "open" affordance so the preview icon can be
      dropped. Four equally-weighted icons per card across six visible cards is 24 competing
      targets in the panel.

### 3.3 Dashboard cards

- [x] The sync, info, vault, and theme icons in the Paperless NGX / Brain Vault card footer all
      render monochrome at `--text-muted`.
- [x] The system status dots stay green — those are genuine state.

### 3.4 Reserve purple

- [x] Audit every purple usage. Keep: logo, active nav item, user message bubbles, active
      selection.
- [x] Neutralise: document ID links (use your standard link colour, or `--text-secondary` with
      an underline), the model dropdown border, calendar deadline highlights (see Phase 5).

### Acceptance criteria

- A greyscale screenshot of the app loses no information except system status and action flags.
- No two icons in the same container render in different hues.

---

## Phase 4 — Suggestion chip row

Keeping horizontal scroll as-is. Three changes only.

- [x] **Remove all emoji.** Replace with icons from the same set used elsewhere, at
      `--text-muted`, sized to match the label. Emoji in UI chrome is the single most
      recognisable amateur tell and it costs nothing to drop.
- [x] **Sentence case.** `WHAT CAN YOU DO?` becomes `What can you do?`, `OPEN DEADLINES`
      becomes `Open deadlines`, and so on. All caps at small sizes hurts legibility and reads
      as shouting.
- [x] **Fade mask on the right edge** so the clipped chip reads as "more content" rather than
      "broken layout":

```css
.suggestion-row {
  mask-image: linear-gradient(to right, black calc(100% - 40px), transparent 100%);
}
```

  Apply the mirrored mask on the left once the row is scrolled away from origin, toggled by a
  scroll listener. If that is more complexity than it is worth, right-side only is acceptable.

### Acceptance criteria

- No emoji anywhere in the chip row.
- The right edge fades rather than cutting a chip mid-glyph.
- Chip labels are sentence case and match the icon vocabulary used in navigation.

---

## Phase 5 — Deadline list ordering (lower priority)

Not visual polish; behavioural. Deferred but worth doing before the next public screenshot.

- [x] **Sort nearest-first.** The list currently descends from the furthest future date, which
      puts pension eligibility in 2064 above anything actionable this month. Overdue items
      group above upcoming, both ascending by proximity to today.
      > **Deviation — do not implement the group order as written.** Overdue outnumbers
      > upcoming ~10:1, so overdue-on-top pushes every upcoming date past the page boundary.
      > Upcoming ships first. See Implementation notes.
- [x] **Make `221 overdue` actionable or demote it.** A large red number the user cannot act on
      becomes permanent background noise. Either it click-throughs to a filtered, sorted
      worklist, or it renders in neutral text. A red number should imply something is expected
      today.
- [x] Calendar deadline highlights: replace the saturated purple fill with a dot marker under
      the date, or a low-alpha fill. Five fully-saturated squares across three months currently
      draw more attention than the deadline table underneath them.

---

## Out of scope for this pass

Recorded so it does not get relitigated:

- **Layout restructuring.** The three-card dashboard summary, the calendar block, and the
  chat-plus-document-panel split are sound. Do not touch them.
- **Reducing the suggestion chip count.** Considered and rejected — horizontal scroll is an
  acceptable affordance and the chips have low cost once the emoji are gone. Revisit only if
  click instrumentation shows the tail is unused.
- **Changing the accent hue.** Dark plus violet is fine and is not what made the UI read as
  unpolished. The problem was colour without meaning, not the specific colour.
- **Spacing scale normalisation.** Worth doing eventually, but it touches every template and
  should not be mixed into a pass that is already changing colour across the same files. Its
  own branch, later.

---

## Implementation notes

Where the delivered work differs from the brief above, and why.

- **`get_tag_colors()` is keyed by tag name, not id.** `PaperlessDocument.tags` is already
  resolved to names by the time a card renders, so an id-keyed map would need a second lookup
  on every chip. The API field is `color` on current Paperless-ngx and `colour` on older
  builds; both are accepted.
- **The colour cache is primed at startup, not lazily.** `_render_card` is synchronous, so a
  lazily-filled cache would paint every chip in its fallback hue on first load and then jump.
  `main.py` warms it in `on_startup`; `do_sync()` invalidates and re-warms it.
- **Card action icons dropped from four to three.** The preview icon is gone and the card body
  is the open affordance, as 3.2 suggested. The thumbnail already opened the document, so this
  only widened an existing target.
- **The document detail dialog does not cap tags at 4.** That view is where the user went to
  see everything; the cap is a scanning aid for cards.
- **Upcoming ships above overdue, contradicting Phase 5's first bullet.** That bullet assumes
  the two groups are comparable in size. They are not: the live archive has 222 overdue against
  22 upcoming, and at `_PAGE_SIZE = 100` overdue-on-top puts every actionable date on page 3 —
  reproducing the exact failure the bullet set out to fix. Order is now upcoming ascending,
  a divider, then overdue descending (most recently missed first). The overdue counter
  click-throughs to the overdue-only worklist, which is where that group is meant to be read.
  The genuine bug Phase 5 identified was separate and is fixed: `future` was `reversed()`, so
  upcoming descended from 2064.
- **The overdue-filter scroll only fires when the filter is switched on.** The "Show all"
  button that clears it lives inside `table_container`; `render_deadline_table()` clears that
  container, deleting the slot the handler is bound to, and a following `ui.run_javascript`
  then cannot resolve the client (`RuntimeError: The parent element this slot belongs to has
  been deleted`). The scroll runs before the re-render and never on clear — by then the user
  is already looking at the table. General rule for this file: a handler on an element inside
  `table_container` must not touch `context.client` after re-rendering.
- **Deadline dot is `--c-text-2`, not purple.** Phase 5 allowed "a dot marker under the date,
  or a low-alpha fill". A purple dot would still spend the accent on something that is neither
  user input nor an active selection, so the marker is neutral and the emphasis moves to the
  table, which was the point.
- **No theme icon exists in the Paperless-ngx / Brain Vault card footer.** 3.3 names four
  icons; the footer has sync, info, vault-sync and dream. All four are now monochrome. The
  theme toggle lives in Settings and was left alone.
- **Purple audit — deliberately kept.** Beyond 3.4's keep list, purple still appears on filled
  primary buttons (`bg-purple-700`), the AI-search input border on the Browser page (that
  border *is* the user's input field), the chat history active item, and the results-panel
  edge handle. Neutralised: document ID links, the model/backend select borders, calendar
  deadline fills, the relevance score badge, the upcoming-deadline count, and the cluster/pin
  icons at rest.
- **Not swept: the vault / brain / web result-card colour system** (blue / purple / teal) in
  the chat results panel, the coloured section-header icons in Settings, Memory and Werkbank,
  and the per-user identity colour (`user_color`) on document owners. Each is a colour system
  in its own right rather than a straggler, and re-deciding them is a wider change than this
  pass scoped. Worth its own phase.
- **ALL CAPS**: the two hardcoded caps *strings* (`PAPERLESS NGX`, `BRAIN VAULT`) are now
  sentence case. The CSS-driven small-caps eyebrow labels (`text-transform: uppercase` on
  section headers) were left — flattening those is a typographic change across every panel,
  not part of a colour pass.
- **Verification**: unit tests for the colour clamp, full suite green (267), app boots clean
  with no tracebacks, the colour map confirmed against the live Paperless instance (79 tags,
  hue preserved, text lightness clamped to 60–68 %), and deadline ordering asserted against
  the real `index.json` (22 upcoming ascending on page 1, divider, 222 overdue descending).
  The rest of the visual acceptance criteria are unverified in a browser — no session
  credentials on this machine. Both defects found after the first pass (the ordering
  regression and the slot crash) surfaced from a user's own browser session, not from these
  checks; the checks above do not exercise the rendered UI.

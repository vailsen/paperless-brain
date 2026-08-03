# Changelog

All notable changes to PaperlessBrain are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/) from `v0.2.0` onward.

## [0.3.0] — 2026-08-03

A visual discipline pass over the whole UI, plus three fixes that landed after
`v0.2.0` and had not yet shipped in an image.

The organising rule, now recorded as an invariant in `CLAUDE.md`: **colour
encodes state, not identity.** Tag chips are the single exception — their hue
comes from Paperless, the user assigned it, and it is what makes tags groupable
across documents at a glance.

### Fixed

- **Sync log went blank mid-run.** The log was appended straight to the dialog
  column, so a websocket drop — routine on a document that takes twelve minutes
  — swallowed every later line. The page froze on whatever it had received and
  looked like a hung sync while the task ran to completion server-side.
- **A slow Paperless could abort a running sync.** The metadata map refresh ran
  on httpx's 5 s default timeout, which a Paperless busy with its own consumer
  exceeds. The resulting `ReadTimeout` propagated into the caller. It now has a
  30 s timeout and serves the previous maps when a refresh fails.
- **Buttons and checkboxes rendered blue.** Quasar's `primary` was never set, so
  it stayed the stock blue, and `ui.button` defaults to `color='primary'`. The
  rule `.bg-primary{…!important}` then beat the `bg-purple-700` classes the
  pages set — twenty buttons read purple in the source and rendered blue on
  screen. `primary` is now the brand purple.
- **Sentence case never rendered.** Quasar ships `text-transform: uppercase` on
  `.q-btn` and `.q-tab`, so editing a label string had no visible effect.
  Overridden once, globally.
- **Deadline list buried everything actionable.** Upcoming deadlines were sorted
  descending from the furthest future date, putting a 2064 pension date above
  anything due this month.
- **"Show all" crashed the deadline table.** The button lives inside the
  container its own handler clears, so the follow-up `ui.run_javascript` could
  not resolve the client through the deleted slot.
- **Relevance was reported inverted.** The figure shown was a raw ChromaDB
  cosine *distance* — lower is better — displayed under a "Relevance" heading
  and sorted ascending, so a correctly ordered list read as least-relevant
  first. It is now converted to a percentage and hidden by default.
- **Unreadable text on filled buttons.** Light-purple labels on the accent
  background measured 3.2:1, below WCAG AA. Now white.
- **Progress ring read as complete at 98 %.** `stroke-linecap="round"` added
  8 px of cap against a ~5 px gap.

### Changed

- **Tag chips** keep their Paperless hue but are clamped to a fixed perceptual
  lightness in OKLCh, so no tag can outshout another. Cards show at most four,
  sorted alphabetically so the visible set is stable across renders, with the
  remainder behind a muted `+N` that expands on click.
- **`Actions` is now `Action required`**, moved out of the tag row onto its own
  line with warning semantics. A noun did not say what was being asserted, and
  system state does not belong among the user's own tags.
- **Icons are monochrome.** Nav, card actions, dashboard footers and section
  headers all inherit the neutral text ramp. The only coloured icons left encode
  state: sync-running, no-model-configured, task done, and the pinned marker.
- **Purple means two things**, the active selection and the user's own input.
  Neutralised elsewhere: document ID links, model and backend select borders,
  calendar deadline fills, relevance badges, and the per-user identity hue that
  used to colour document owners.
- **The active nav item is marked** with the accent plus an underline, so the
  state survives a greyscale screenshot and colour-blind viewing.
- **A document card opens from its body**, which retired the preview icon. Three
  icons per card across eight cards is a lot of competing targets.
- **Suggestion chips** use icons from the app's own set instead of emoji, read
  in sentence case, and fade at the scroll edges rather than clipping a chip
  mid-glyph.
- **Deadlines** list upcoming first, nearest first, then overdue most-recently
  missed first. The overdue counter click-throughs to a filtered worklist.
- **The calendar** marks a deadline with a dot under the date instead of a
  saturated fill.
- **The sync status card** drops the progress ring — `461 / 467` and
  `98 % indexed` already sat beside it — and its labels are sentence case
  (`Paperless-ngx`, `Brain vault`) with monochrome sync, log, vault and dream
  icons.

### Added

- `SHOW_RELEVANCE_SCORES` — debug flag, off by default, surfaces retrieval
  scores on result cards and in the detail dialog.
- `app_ui/tag_style.py` — the single path for rendering a tag chip, with the
  Paperless colour map cached for 15 minutes, primed at startup and invalidated
  on manual sync.
- Unit tests for the colour clamp and the relevance direction.
- A UI visual discipline section in `CLAUDE.md`, plus the two task documents the
  pass was worked from under `docs/`.

### Documentation

- Install instructions no longer require cloning the repository.

## [0.2.0] — 2026-08-02

First semantically versioned release. See the
[release notes](https://github.com/vailsen/paperless-brain/releases/tag/v0.2.0).

[0.3.0]: https://github.com/vailsen/paperless-brain/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vailsen/paperless-brain/releases/tag/v0.2.0

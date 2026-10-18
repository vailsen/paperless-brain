# Images in chat — implementation plan (handover)

Hand-over for a Claude Code session on the dev machine, which can see the real
archive. Written 2026-10-18 from a design conversation with the owner. Read this
whole file before touching code; the decisions in §1 are made, the open points in
§10 are not.

## 1. Goal and decisions (made with the owner)

The owner wants to ask the chat things like "show me the lasagna recipe" about a
scanned ~50-page recipe magazine and get the **picture** back, not only the text.

Decided:

1. **Show a whole page as an image in the chat.** The cheap path, works for every
   document that exists today, no re-ingest needed. Build this first.
2. **New sidecar field per page:** `images: [{bbox, description, source}]`,
   `source` = `"pdf"` (exact box read from the PDF) or `"model"` (box returned by
   the vision model). Filled at ingestion.
3. **Scanned PDFs are the main case** — at least half of the archive, and the
   magazine is a scan. So the vision model's boxes are the primary path; the
   `"pdf"` path is a cheap bonus for born-digital files.
4. **One chat tool** that shows either a whole page or one cropped picture.
5. **A mechanism that re-ingests existing documents** so they get the new
   sidecar format.

## 2. What the code and the data say today

Verified in the code (paths as of `main` d7e6a80):

- Ingestion renders each page with `services/pdf_extractor.py`
  (`PDFExtractor.extract_pages`, long side ≈ `TARGET_LONG_SIDE_PX = 2600`, 150–300 DPI,
  JPEG) → `PageImage(page_number, total_pages, image_bytes)` (`models/extraction.py`).
- Each page goes to `VisionClient.analyze_document` (`services/vision.py`); output is
  constrained by `EXTRACTION_JSON_SCHEMA` (`config/extraction_rules/base.py`) for all
  three transports (Ollama `format`, OpenAI `response_format`, Anthropic tool schema)
  and parsed in `VisionClient._parse_fields`.
- Images are described **inline in `page_text`** today: rule 1b of `BASE_INSTRUCTIONS`
  asks for a paragraph `"[Image description: ...]"`.
- `JsonSidecar` (`models/extraction.py`) has no image field. `PROMPT_VERSION = "0.2"`
  in `config/extraction_rules/base.py`.
- Re-ingest of stale documents already exists: `SidecarService.outdated_ids(PROMPT_VERSION)`
  + `do_sync(reingest_limit=…)` in `app_ui/pages/dashboard.py` (delete + ingest, capped
  per sync by a number field on the dashboard).
- Chat tools: `TOOL_DEFINITIONS` in `services/chat_service.py`.
  `view_document_page` = *visual analysis* (sends a page to the vision model and
  returns text; slow; tool group `"visual"`, off by default). `get_document_page_text`
  returns `page_text` from the sidecar after a permission check via `_user_paperless()`.
- Tools that make the UI do something return an event, e.g. `DownloadRequestEvent`
  (`services/chat_service.py`), handled in `app_ui/pages/chat.py` (`isinstance(event,
  DownloadRequestEvent)` in the turn loop). Use the same pattern.

From the owner's real data (counted by the dev-machine session):

- 523 ingested documents, 1813 pages. Only **9 documents / 15 pages** carry an image
  description — the model describes images rarely.
- The inline marker is **not stable**: 8× `Image description`, 6× `Bildbeschreibung`,
  1× `Abbildung` — the model translates the marker in German documents. Anything that
  parses page text for images is unreliable. A structured field fixes both problems.

Verified by experiment (pypdfium2 5.14, in the dev container): for a born-digital PDF,
`page.get_objects(filter=[pypdfium2.raw.FPDF_PAGEOBJ_IMAGE], max_depth=3)` returns the
embedded pictures, `obj.get_bounds()` gives `(left, bottom, right, top)` in PDF points,
and `page.render(scale=…, crop=(left, bottom, W-right, H-top))` renders exactly that
area. Note: the method is `get_bounds()`, not `get_pos()`. A scanned page shows up as
**one image object covering ~the whole page** — that is the signal to fall back to the
model's boxes.

## 3. Phases

Build and ship in this order; each phase is useful on its own.

| Phase | What | Re-ingest needed |
|---|---|---|
| A | Whole page as image in chat (tool + route + UI) | no |
| B | `images[]` at ingestion (schema, prompt, boxes, sidecar, chunks) | new docs only |
| C | Cropped pictures in chat (tool mode, search hints) | uses B |
| D | Re-ingest existing documents into the new format | — |
| E | Docs, i18n, CLAUDE.md | — |

Run the **calibration in §6 before phase D** and before tuning anything in phase B.

## 4. Design

### Phase A — whole page in chat

**Image route** (new module, e.g. `app_ui/doc_image_routes.py`, registered like the
other routes — see `app_ui/vault_routes.py` / `app_ui/memo_routes.py` for how routes
are added and how they read the session):

- `GET /api/doc-image/{doc_id}/{page}` → JPEG of the whole page.
  `GET /api/doc-image/{doc_id}/{page}?i={index}` → crop of `images[index]` (phase C).
- **Permission:** resolve the user from the session exactly like the other routes;
  check access with the user's own Paperless client (`list_documents(ids=[doc_id])`
  non-empty, same as `_tool_get_document_page_text`). No session or no access → 404
  (not 403 — do not confirm a document exists).
- Get the PDF with the **user's** client (`download_document`), render the page with
  pypdfium2 at a display resolution (long side ~1600 px is plenty for a chat bubble;
  do not reuse the 2600 px ingest scale), encode JPEG quality ~85.
- **Cache** rendered results on disk, e.g. `data/image_cache/{doc_id}/{page}[_{i}]_{w}.jpg`,
  keyed also by the document's `modified` timestamp so a re-scan invalidates it.
  Cap total size (e.g. 500 MB, LRU by mtime). Response header
  `Cache-Control: private, max-age=86400`.
- Page out of range → 404.

**Chat tool** — one tool, two modes (do *not* add two tools; the project keeps tool
count low because local models pick worse with more tools):

```
show_document_image(document_id: int, page_number: int, image_index: int | None)
```

- No `image_index` → whole page. With index → that picture (phase C).
- Server side it only validates (permission, page exists, index exists) and returns a
  new `ImageDisplayEvent(doc_id, page, index, caption)` plus a short text result for the
  model ("Shown to the user: page 12 of document #345.").
- The model never sees the pixels — this tool is for the user's eyes. It must not be
  confused with `view_document_page` (visual analysis). Say so in both descriptions.
- Put it in the **`documents`** tool group (always on), not in `"visual"` (off by
  default). It is cheap: no model call.
- Label for the activity bar in `_tool_label()` (translated), e.g. `"🖼 Showing image..."`.

**Chat UI** (`app_ui/pages/chat.py`): on `ImageDisplayEvent`, add the image to the
assistant message. Recommended: append Markdown
`![caption](/api/doc-image/{doc}/{page}?i={i})` to the message text so it survives the
chat history and reloads (history stores Markdown). Then check:

- History is stored as message text in SQLite (`services/chat_history_service.py`,
  `chat_messages.content`) and replayed to the model on later turns, so the Markdown
  image line reaches the model as text — harmless, but keep the caption short.
- `generate_chat_pdf` / chat-to-PDF: either fetch and embed these images server-side or
  strip them — do not leave relative URLs WeasyPrint cannot resolve.
- **Phone width:** images must get `max-width:100%; height:auto` inside the chat bubble
  (CLAUDE.md "UI visual discipline" — nothing may scroll sideways). Verify with
  Playwright at 390 px, as CLAUDE.md requires.
- Click/tap opens the full-size image (new tab is fine).

**Prompt** (`config/chat_prompts.py`, `documents` block): one or two lines — call
`show_document_image` when the user asks to see something, or when the answer is about
something visual (a dish, a product, a photo, a diagram); at most 3 images per answer;
never for invoices/letters unless asked.

**Werkbank:** this tool is display-only and must not be offered to deep-research
agents. Check `config/agents.yaml` (tool lists are explicit, so it should not appear by
itself) and `werkbank/v2/tools.py` (`SEARCH_TOOLS`, tool trust) — add nothing there.

### Phase B — `images[]` at ingestion

**Schema** (`EXTRACTION_JSON_SCHEMA`), new optional array per page:

```json
"images": {
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "description": {"type": "string"},
      "belongs_to":  {"type": "string"},
      "box": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
    },
    "required": ["description", "box"]
  }
}
```

- `box` = `[x1, y1, x2, y2]` **normalized 0–1000** relative to the page image the model
  was shown (top-left origin). Normalized because the model never knows our render size,
  and because Qwen-VL-style models are trained on this convention.
- `belongs_to` = what the picture illustrates ("Recipe: Lasagne al forno"), may be "".
- Keep `required` in the top-level schema unchanged (images optional) so a model that
  returns no images still validates.

**Prompt** (`BASE_INSTRUCTIONS` rule 1b): stop writing `[Image description: …]` into
`page_text`; put every **photo, illustration, diagram or chart** into `images` instead —
not logos, icons, decorative lines or the page background. Describe in the archive
language. Give the box rule and one example in the prompt. Keep the rest of the prompt
unchanged. Bump `PROMPT_VERSION` (→ `"0.3"`) **only in phase D**, when re-ingest is
wanted.

**Box sources** in `pipelines/ingest.py`, per page:

1. **`source: "pdf"`** — read embedded image objects with pypdfium2 (§2). Skip tiny ones
   (< 2 % of page area: logos, icons). If there is exactly one image covering > 85 % of
   the page, it is a scan — ignore it and use the model's boxes. When PDF boxes exist,
   still let the model describe the pictures; match model entries to PDF boxes by
   overlap (IoU) and keep the exact PDF box.
2. **`source: "model"`** — the model's `box`.

**Validate every box in code** (never trust it): clamp to 0–1000; require x2 > x1 and
y2 > y1; drop boxes under 1 % or over 98 % of the page; drop exact duplicates.
**Unit detection:** some models (Qwen2.5-VL) answer in *pixels of the image they saw*
instead of 0–1000. If any coordinate exceeds 1000, convert with the rendered page size
(known from the rendered image) before validating. The calibration in §6 decides
whether this matters for the owner's model.

**Sidecar:** store per page in `pages[i]["images"]` as
`{"bbox": [x, y, w, h], "description": "...", "belongs_to": "...", "source": "model"}`
with `bbox` as **fractions of the page (0–1, top-left origin)** — resolution-free, so
crops can be rendered at any size later. Add `images: list[dict] = []` handling to
`ExtractedContent` and keep `JsonSidecar` backward compatible (old sidecars have no key;
every reader uses `.get("images") or []`).

**Search:** image descriptions must stay findable, but no longer via the unstable inline
marker. When building chunks (`services/chunking.py` → `chunk_document`, called from
`ingest.py`), append one line per picture to that page's text **for embedding only**:
`[Image {n}: {description} — {belongs_to}]`, with a fixed English marker regardless of
language. Do not put these lines into `full_text` / `page_text` — that text is pushed
back into Paperless by the opt-in text write-back (`pipelines/text_push.py`).

**Tool output:** `get_document_page_text` and `get_document_details` should list the
pictures of a page/document with their index and description, e.g.
`Images on page 12: [0] Lasagne in a baking dish (Recipe: Lasagne al forno)`, so the
model knows which `image_index` to pass. Search results (`_tool_search`) may add
`📷 3 images` per hit.

### Phase C — cropped pictures

- Route: `?i={index}` → render the page, crop `bbox` (fractions × rendered size) with
  ~2 % padding, JPEG. Index out of range → fall back to the whole page, never 500.
- Tool: `image_index` given → validate it exists in the sidecar; otherwise answer with
  the available indices.

### Phase D — re-ingest existing documents

Use the mechanism that exists rather than a new one: bump `PROMPT_VERSION` to `"0.3"`.
Every sidecar with `prompt_version != "0.3"` is then "outdated", the dashboard shows the
count, and each sync re-ingests up to the number in the dashboard's re-ingest-limit
field (`do_sync(reingest_limit=…)`).

Things to know and to tell the owner:

- **Cost:** 1813 pages through the local vision model. Measure seconds per page on the
  owner's machine during calibration and give a real estimate before bumping the
  version. Re-ingest in batches via the limit field.
- **Order:** consider re-ingesting the 9 documents that already have image descriptions
  and the magazine first (e.g. sort `outdated_ids` so documents with images come
  first, or add a "re-ingest this document" path — the document dialog already has
  "Re-ingest (extraction + embedding)").
- **Re-ingest is delete + ingest.** If ingest fails mid-way, the document is missing
  from the index until the next sync retries it — existing behaviour, but mention it.
- Not affected by re-ingest: the AI title records (`data/title_renames.json`, kept
  outside the sidecar folder on purpose). Possibly affected: action/deadline review
  verdicts (`action_review.json`, keyed by action text) — re-extracted actions with
  different wording get reviewed again, which costs model calls.
- The text write-back pushes the *new* page text (now without inline image
  descriptions) on the next sync — an improvement, but a visible change in Paperless.

### Phase E — docs, i18n, CLAUDE.md

- Every new UI string through `_()`, German in `locales/de/LC_MESSAGES/messages.po`,
  `pybabel extract → update → compile` (see CLAUDE.md).
- `docs/` + README feature bullet ("Pictures in chat").
- CLAUDE.md: a short section with the invariants of §8.

## 5. Files you will touch (expected)

`config/extraction_rules/base.py`, `models/extraction.py`, `services/vision.py`
(`_parse_fields`, schema passing per transport), `services/pdf_extractor.py` (expose
rendered size / a display render helper), `pipelines/ingest.py`, `services/chunking.py`,
`services/chat_service.py` (tool, event, tool label, page-text/details output),
`config/chat_prompts.py`, `app_ui/pages/chat.py`, new `app_ui/doc_image_routes.py`,
route registration in `main.py` (an `import app_ui.doc_image_routes` line, like `memo_routes` / `vault_routes`), tests, locales, docs, CLAUDE.md.

## 6. Calibration — before phase D, with the real model

The owner's archive is mostly scans, so everything hinges on how well **their** vision
model draws boxes. Before re-ingesting anything:

1. Pick 5 scanned pages: 3 from the recipe magazine (one with several photos), one
   letter with a logo, one with a diagram or photo.
2. Run only the extraction (no sidecar write, no embedding) and save, per page: the raw
   model JSON, and every crop as a file next to the page render.
3. Look at the crops with the owner. Check: are boxes in 0–1000 or pixels? Do they
   cover the picture or drift? Are logos/decorations filtered?
4. Measure seconds per page with and without the `images` field, so the re-ingest
   estimate in phase D is real.

Model guidance: Qwen2.5-VL / Qwen3-VL are trained for this kind of grounding and give
usable boxes; many other local VLMs (LLaVA-family, some Gemma builds) do not. If the
owner's model fails calibration, say so and offer: (a) another model for ingestion, or
(b) keep `images[]` with descriptions but without boxes — the chat then shows the whole
page for those pictures (phase A), which always works.

## 7. Tests

- Box validation: clamping, inverted boxes, too small/too large, pixel→normalized
  conversion, duplicates.
- PDF path: a WeasyPrint-generated PDF with two images → two `"pdf"` boxes; a PDF that is
  one full-page image → treated as a scan.
- Sidecar backward compatibility: old sidecar without `images` loads; every reader
  copes.
- Chunk text gets `[Image n: …]` lines; `full_text` / `page_text` do not.
- Tool: permission denied → not shown; bad page/index → helpful text, no event; good
  call → `ImageDisplayEvent`.
- Route: no session → 404; foreign document → 404; whole page and crop return JPEG;
  cache hit on second call.
- Werkbank: the new tool is not in any archetype's tool list.
- Playwright: image in a chat bubble at 390 px, no horizontal scroll
  (`document.documentElement.scrollWidth == 390`).

## 8. Invariants (put these in CLAUDE.md)

- **Boxes come from code where possible, from the model otherwise, and are always
  validated in code.** A model box is a claim, not a fact.
- **Coordinates are stored resolution-free** (fractions of the page, top-left origin).
- **Image descriptions live in `images[]`, never inline in `page_text`.** The inline
  marker was translated by the model and is unparseable; the structured field is the
  only source.
- **Every image request checks the user's own Paperless access** — sidecars and the
  cache have no access control.
- **`show_document_image` is display-only.** The model never receives the pixels from
  it; that is what `view_document_page` (visual analysis) is for.
- **Re-ingest goes through `PROMPT_VERSION`**, not a second versioning mechanism.

## 9. Done means

Phase A: the owner asks for a page of the magazine and sees it inline, on desktop and
phone. Phase B–C: a newly ingested magazine page yields a cropped photo for "show me the
lasagna". Phase D: the dashboard shows the outdated count, and a batch re-ingest
produces `images[]` in the new sidecars. Tests, lint and CI green; PR per phase or one
PR with clearly separated commits.

## 10. Open — ask the owner

1. Which vision model ingests today (Settings > Documents > Processing new documents)?
   Calibration result decides whether boxes are worth it with that model.
2. Re-ingest all 523 documents, or only new ones plus a chosen few (magazine, the 9 with
   images)? The time estimate from calibration should drive this.
3. Should images appear in chat-to-PDF exports, or be left out?

## 11. Status and calibration result (2026-10-18)

Phases A–C are built; phase D is prepared (re-ingest ordering) and switched on by
`PROMPT_VERSION = "0.3"`.

Calibration on the owner's ingest model `qwen3.8:27b-mtp-q4_K_M` (OpenAI-compatible
endpoint on Ollama), six real pages, new prompt vs old prompt:

| Page | Kind | Result |
|---|---|---|
| #361 p1 | X-ray, born-digital | model box ≈ PDF box → stored as exact `"pdf"` box |
| #577 p3 | two bar charts, scan | two tight `"model"` boxes |
| #126 p25 | map, born-digital, stored as **two image strips** | strips merged, matched → `"pdf"` |
| #700 p1 | invoice with a 0.08 % product thumbnail + 11 % letterhead logo | thumbnail boxed correctly; logo ignored |
| #647 p2 | photo collage, scan | one `"model"` box |
| #668 p1 | letter with seal and QR code | `images: []` — exclusion rule works |

- Boxes come on the 0–1000 grid, not in pixels.
- The old inline marker no longer appears in `page_text`.
- Time per page: 13.6 s new vs 15.4 s old (average of six) — no extra cost.
  A full re-ingest of 523 documents / 1813 pages ≈ 7 h of GPU time plus one summary
  call per document; do it in batches with the dashboard's re-ingest limit.

Decisions taken from the data: the PDF never decides what is a picture (the logo
case); `MIN_AREA` is 0.05 % of the page (the thumbnail case); touching PDF image
objects are merged (the map case). Chat-to-PDF embeds the pictures (§10.3).

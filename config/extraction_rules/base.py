# config/extraction_rules/base.py

"""Shared scaffolding for all extraction profiles.

Profile-independent: the JSON schema the vision model must emit, the base
instructions prepended to every per-type prompt, and the condensed-summary
prompt. The per-document-type rules live in the profile modules (de.py, en.py).
"""

PROMPT_VERSION = "0.6"
# 0.3: pictures in a structured "images" field, not inline
# 0.4: formulas as LaTeX — re-ingests only pages that show one
#      (sidecar_service._SCOPED_CHANGES); change nothing else in the same step
# 0.5: stamps, seals and signatures are pictures (initials in the margin are
#      not) — a full re-ingest; which page has a signature cannot be told
#      from the old text
# 0.6: letterhead and footer TEXT stays in page_text (Qwen3.8 dropped IBAN,
#      phone and opening hours as "letterhead"); picture descriptions in the
#      document's language

CONDENSED_SUMMARY_PROMPT = """\
You receive the page summaries of a scanned document — one summary per page.

Create a concise overall summary of the document in 2–4 sentences.
Write from the perspective of a reader who has never seen the document.
Focus on the essentials: What is it about? What is the result or core statement? Are there important amounts, deadlines or parties?

Ignore meaningless pages such as privacy notices, terms-and-conditions appendices, mandatory legal boilerplate, empty pages or pure form pages without informative content.

Respond exclusively in {language} and only with the summary text — no JSON, no heading, no introduction.\
"""

# Asks for the two parts of a document title the archive does not already know.
# Date and correspondent come from Paperless — the model cannot get them wrong
# that way — so it is told not to repeat them. Two labelled lines rather than
# JSON: this goes through the free-text path of every transport, and a local
# model answering "TYPE: …" is far more reliable than one emitting JSON unasked.
TITLE_DESCRIPTION_PROMPT = """\
You receive the summary of a scanned document. Name it for a file name.

Answer with exactly two lines and nothing else:
TYPE: the kind of document in 1 word (e.g. invoice, contract, notice, payslip — Rechnung, Vertrag, Bescheid, Gehaltsabrechnung)
SUBJECT: what it is about in at most 3 short words

Rules for SUBJECT:
- Name the category, not the items. An invoice for CPU, graphics card, RAM and SSD is "computer parts" (German: "Computerteile"); groceries are "groceries" ("Lebensmittel"); several insurance policies are "insurance" ("Versicherungen").
- Only words that carry meaning: no "and", "or", "of", "for", "und", "oder", "für", "von", no articles.
- Prefer short, common words and usual abbreviations (e.g. "car insurance" / "Kfz-Versicherung", "tax return" / "Steuererklärung").
- Do not put the sender, a date or an amount into either line — those are added separately.

Write both lines in {language}. No quotes, no punctuation at the end.\
"""

BASE_INSTRUCTIONS = """\
You are analyzing a scanned document (JPEG of a single page).
Some scans contain two document pages side by side (double-page scan):
then extract BOTH halves completely — first the entire left half,
then the right. Do not skip any half or column.

Tasks:
1. Extract the complete page text as clean running text.
   - Separate meaningful paragraphs with a blank line (\n\n). A new paragraph starts at a change of topic, a new section or a new unit of meaning.
   - Multi-column layouts: extract column by column in correct reading order.
     If labels are on the left and their values/units in separate columns
     further right, assign each label its value and unit correctly
     (align row by row).
   - Forms, data sheets, certificates with fields (e.g. numbered items):
     copy each field VERBATIM as its own line "field name: value unit".
     Do NOT rephrase into prose, do NOT summarize, do NOT interpret
     or omit values. Render empty fields as "-".
   - Tables: render row by row as compact lines "label: value unit" in reading
     flow. Values exactly as shown, no rephrasing into sentences.
   - Pictures (photos, illustrations, diagrams, charts, maps, X-rays, drawings)
     do NOT go into page_text — they go into "images" (task 1b). Text printed
     around a picture (headings, captions, recipe text) stays in page_text.
   - Mathematical formulas and equations: write them as LaTeX — inline
     between $…$, a formula set apart on its own line between $$…$$
     (e.g. $$U = R \\cdot I$$, $\\sigma = \\frac{F}{A}$). Copy every symbol, index
     and exponent exactly; do NOT simplify, solve or rename anything. Values
     with units are not formulas: "5 m²", "210/80 mmHg", "19 % VAT" stay plain text.
   - Logos and decorative elements: skip. The TEXT of a letterhead or footer
     stays: sender, address, phone, e-mail, bank details (IBAN, BIC),
     opening hours, register numbers.

1b. List EVERY picture on the page in "images" — one entry per picture:
   photos, illustrations, diagrams, charts, maps, X-rays and other medical
   images, technical drawings, product images, stamps, seals and handwritten
   signatures. NOT pictures: logos, letterheads and the coats of arms or
   emblems in them, icons, barcodes, QR codes, initials in the page margin,
   decorative lines and frames, the page background. A page without
   pictures has "images": [].
   - "description": in the language of the document. Describe the picture
     in detail — it is the only place the picture exists as text. Especially
     on pages that consist mostly of pictures, the descriptions are the main
     content of the page.
   - "belongs_to": what the picture illustrates or belongs to, in a few words
     (e.g. "Recipe: Lasagne al forno", "Heating costs 2025", "Lumbar spine
     follow-up"). "" when nothing on the page says.
   - "box": where the picture is, as [x1, y1, x2, y2] — left, top, right,
     bottom edge on a 0–1000 grid over the whole page image: 0,0 is the
     top-left corner, 1000,1000 the bottom-right. The box encloses the
     picture itself, not its caption. Example: a photo filling the upper
     right quarter of the page is [500, 0, 1000, 500].
   - Two pictures side by side are two entries; one picture is one entry,
     even if it is large.
   What to put into a description:
   - In general: What is depicted? Perspective/view, recognizable objects,
     copy labels and measured values in the image verbatim.
   - Food and dishes: name the dish, what is visible, how it is served.
   - Charts: chart type, axes, every labelled value verbatim.
   - Medical images (X-ray, MRI, CT, ultrasound): name modality and
     body region, visible anatomical structures, implants/
     instrumentation (screws, rods, cages, prostheses), orientation
     (a.p./lateral, standing/lying). Give your best professional assessment
     of recognizable findings (e.g. vertebral slippage, fractures, wear,
     position of the hardware) — phrased as an image impression ("shows",
     "consistent with"), not as a confirmed diagnosis. Mark uncertainty.
   - Stamps, seals, signatures: the legible imprint text; whose signature
     only if a printed name next to it says so.
   - Technical drawings/plans: depicted part/object, views
     (top view, section), dimensions and tolerances verbatim, parts lists,
     material specifications, scale.
   Better a detailed assessment with marked uncertainty than
   no description at all.

2. Extract tables with structurally queryable data as a JSON array.
   Each table has: caption (short description), rows (list of dicts).
   The table's column headers are the dict keys, the cell values the dict values.
   Output strings that obviously represent numeric values as decimals with a dot (1014.01, not 1.014,01).
   Example for a table with columns "Service", "Reimbursement", "Maximum":
   {"caption": "Benefits overview", "rows": [{"Service": "Dental cleaning", "Reimbursement": "80%", "Maximum": "200.50 EUR/year"}]}
   IMPORTANT:
   - COMPLETENESS: capture EVERY row and EVERY value of the table — even long
     tables completely, never just a selection.
   - Labeled value lists without a table frame (e.g. "Low: 193 g/km,
     Medium: 145 g/km, ...") are tables too — capture as rows with columns like
     "Category", "Value", "Unit".
   - Copy values exactly as shown (incl. units). Unit as its own
     column "Unit" when it has its own column in the table.
   - Output empty cells as "", cells containing "-" as "-".
   - Form fields ("field name: value") that already appear as lines in the page
     text must NOT be duplicated as a table — capture only real tabular
     structures (several columns × several rows) and labeled value lists.
   - Do NOT output a table with an empty rows array.
   - Set "continued_from_previous_page": true ONLY when the table starts at the
     VERY TOP of the page, has NO heading of its own and obviously continues the
     table cut off at the end of the previous page — then use exactly the column
     names of the previous page's table (see context below, if present). A table
     with its own heading or further down the page is NEVER a continuation;
     omit the field there or set it to false.

3. Extract calls to action and deadlines that concern the recipient.
   Each action has: description, deadline (ISO date), deadline_certain (bool).
   For dates without a year: derive from the document context and set deadline_certain=false.
   IMPORTANT: capture only real calls to action directed at the recipient!
   - YES: "Please transfer by June 30", objection deadlines, return deadlines
   - NO: the document's issue date, invoice date, date of a correction,
     periods ("billing period 01.01–31.12"), purely informational dates
   Do NOT capture calls to action without a concrete date or deadline.
   General notes ("please keep on file", "please inform") are not actions.

4. Create a summary of the page in exactly one sentence.

5. Extract cross-references to other documents:
   file numbers, case numbers, policy numbers, invoice numbers
   that refer to other paperwork.

Respond exclusively with valid JSON in the following schema:
{
  "page_text": "str",
  "tables": [{"caption": "str", "continued_from_previous_page": true/false, "rows": [{"Column1": "Value1", "Column2": "Value2"}]}],
  "actions": [{"description": "str", "deadline": "YYYY-MM-DD", "deadline_certain": true/false}],
  "page_summary": "str (one sentence)",
  "cross_references": [{"type": "str", "value": "str"}],
  "images": [{"description": "str", "belongs_to": "str", "box": [x1, y1, x2, y2]}],
  "document_type": "str (only on page 1, else null)"
}
"""

# JSON schema for Ollama structured output (format=): guarantees parseable JSON
# even on dense pages where free-form generation drifts. Table rows keep free-form
# keys (column headers vary per table), hence a generic object.
EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "page_text": {"type": "string"},
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string"},
                    "continued_from_previous_page": {"type": "boolean"},
                    "rows": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["caption", "rows"],
            },
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "deadline": {"type": "string"},
                    "deadline_certain": {"type": "boolean"},
                },
                "required": ["description", "deadline", "deadline_certain"],
            },
        },
        "page_summary": {"type": "string"},
        "cross_references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["type", "value"],
            },
        },
        # Optional on purpose: a model that returns no images still validates,
        # and a missing key means "no pictures", never a failed page.
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "belongs_to": {"type": "string"},
                    "box": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
                "required": ["description", "box"],
            },
        },
        "document_type": {"type": "string"},
    },
    "required": ["page_text", "tables", "actions", "page_summary", "cross_references"],
}

# Appended to the page prompt when the previous page ended with a table, so the
# model can mark and structurally align a continuation on the current page.
TABLE_CONTINUATION_CONTEXT = """

Context: the previous page ended with the following table:
- caption: "{caption}"
- columns: {columns}
If this page starts at the top with the continuation of this table, extract the
continuation rows as their own table with EXACTLY the same column names and set
"continued_from_previous_page": true there.
"""

# Appended to every page prompt of a document whose owner left a reading hint
# when re-ingesting it (services/ingest_hints.py). Only present for those
# documents, so it is not part of PROMPT_VERSION: the base prompt is unchanged.
USER_READING_HINT = """

Reading hint from the owner of this document. They have compared an earlier
extraction with the original and found something misread:
<<<
{hint}
>>>
Take it into account wherever it concerns this page and ignore it where it does
not. It tells you how to read the page; it is not content of the page. Never copy
it into page_text, and never write a value the page does not show.
"""

# ---------------------------------------------------------------------------
# Per-document-type additional instructions — default "DE profile".
# Keys must match the document_type names of your Paperless instance; the rule
# texts are German (German legal/administrative domain). Add or replace entries
# to match your own archive.
# ---------------------------------------------------------------------------


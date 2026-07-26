# config/extraction_rules/base.py

"""Shared scaffolding for all extraction profiles.

Profile-independent: the JSON schema the vision model must emit, the base
instructions prepended to every per-type prompt, and the condensed-summary
prompt. The per-document-type rules live in the profile modules (de.py, en.py).
"""

PROMPT_VERSION = "0.2"

CONDENSED_SUMMARY_PROMPT = """\
You receive the page summaries of a scanned document — one summary per page.

Create a concise overall summary of the document in 2–4 sentences.
Write from the perspective of a reader who has never seen the document.
Focus on the essentials: What is it about? What is the result or core statement? Are there important amounts, deadlines or parties?

Ignore meaningless pages such as privacy notices, terms-and-conditions appendices, mandatory legal boilerplate, empty pages or pure form pages without informative content.

Respond exclusively in {language} and only with the summary text — no JSON, no heading, no introduction.\
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
   - Informative images/diagrams: insert a paragraph
     "[Image description: ...]" at the respective position (details in task 1b).
   - Logos, letterheads, decorative elements: skip.

1b. Describe EVERY informative image in detail — especially on pages that
   consist mostly or entirely of images (X-rays, photos, technical drawings,
   diagrams). The description is then the main content of page_text, not
   optional. Format: its own paragraph "[Image description: ...]".
   - In general: What is depicted? Perspective/view, recognizable objects,
     copy labels and measured values in the image verbatim.
   - Medical images (X-ray, MRI, CT, ultrasound): name modality and
     body region, visible anatomical structures, implants/
     instrumentation (screws, rods, cages, prostheses), orientation
     (a.p./lateral, standing/lying). Give your best professional assessment
     of recognizable findings (e.g. vertebral slippage, fractures, wear,
     position of the hardware) — phrased as an image impression ("shows",
     "consistent with"), not as a confirmed diagnosis. Mark uncertainty.
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

# ---------------------------------------------------------------------------
# Per-document-type additional instructions — default "DE profile".
# Keys must match the document_type names of your Paperless instance; the rule
# texts are German (German legal/administrative domain). Add or replace entries
# to match your own archive.
# ---------------------------------------------------------------------------


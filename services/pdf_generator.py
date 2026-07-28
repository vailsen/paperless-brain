# services/pdf_generator.py
"""Generate styled PDF documents from markdown content using WeasyPrint."""

import html
import re
from datetime import datetime

import markdown2
from weasyprint import CSS, HTML

from config.settings import local_tz, settings
from i18n import format_datetime

_URL_RE = re.compile(r'https?://[^\s<>"\[\]{}|\\^`]+[^\s<>"\[\]{}|\\^`.,;:!?)>]')

_TABLE_RE = re.compile(r'<table\b[^>]*>.*?</table>', re.DOTALL | re.IGNORECASE)
_TH_RE    = re.compile(r'<th\b', re.IGNORECASE)


def _process_tables(html_body: str) -> str:
    """Wrap each table in a page-break-avoiding div and scale font by column count."""
    def _replace_table(m: re.Match) -> str:
        tbl = m.group(0)
        col_count = len(_TH_RE.findall(tbl))
        # Scale font down for wide tables
        if col_count >= 7:
            font_pt = "7pt"
        elif col_count >= 5:
            font_pt = "8pt"
        else:
            font_pt = "9pt"
        styled = tbl.replace(
            tbl[:tbl.index('>') + 1],
            tbl[:tbl.index('>') + 1].replace(
                '<table', f'<table style="font-size:{font_pt}"', 1
            ),
            1,
        )
        return (
            f'<div style="page-break-inside:avoid;break-inside:avoid;'
            f'page-break-before:auto;margin:14px 0">'
            f'{styled}'
            f'</div>'
        )

    return _TABLE_RE.sub(_replace_table, html_body)


# Badge text is archive-level (the PDF lands in the shared Paperless archive),
# so it follows ARCHIVE_LANGUAGE, not the per-user UI language.
_BADGE_TEXT = {
    "en": "AI-generated",
    "de": "KI-generiert",
}

_USER_CSS = """
@page {
    size: A4;
    margin: 54pt;
}
body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 11pt;
    color: #1a202c;
    line-height: 1.65;
}
.doc-header {
    margin-bottom: 4px;
}
.doc-title {
    font-size: 18pt;
    font-weight: bold;
    color: #1a202c;
    margin-bottom: 6px;
}
.meta-line {
    font-size: 9pt;
    color: #718096;
    margin-bottom: 4px;
}
.badge {
    font-size: 8pt;
    font-weight: bold;
    color: #6b46c1;
    border: 1px solid #6b46c1;
    padding: 1px 6px;
}
.header-rule {
    border: 1px solid #e2e8f0;
    margin: 10px 0 14px 0;
}
h1 { font-size: 15pt; color: #2d3748; margin-top: 18px; margin-bottom: 4px; }
h2 { font-size: 13pt; color: #2d3748; margin-top: 14px; margin-bottom: 4px; }
h3 { font-size: 11pt; color: #4a5568; font-weight: bold; margin-top: 10px; margin-bottom: 2px; }
p  { margin: 6px 0; }
a  { color: #6b46c1; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 0;
    table-layout: fixed;
    font-size: 9pt;
}
th { background: #f7fafc; font-weight: bold; text-align: left; }
th, td {
    border: 1px solid #cbd5e0;
    padding: 4px 7px;
    /* `word-break: break-word` is invalid CSS and WeasyPrint discards it with a
       warning; overflow-wrap is the standard property and does the same job. */
    overflow-wrap: break-word;
}
tr:nth-child(even) { background: #f7fafc; }
code { background: #f7fafc; padding: 1px 4px; font-family: monospace; font-size: 9pt; }
pre  { background: #f7fafc; padding: 8px; border-radius: 3px; white-space: pre-wrap; }
blockquote { border-left: 3px solid #6b46c1; margin: 8px 0; padding-left: 10px; color: #4a5568; }
ul, ol { margin: 6px 0; padding-left: 18px; }
li { margin: 3px 0; }
"""


def generate_chat_pdf(
    content_markdown: str,
    title: str,
    username: str,
    model_name: str,
    dt: datetime | None = None,
) -> bytes:
    """Render markdown to a styled A4 PDF and return the bytes."""
    if dt is None:
        dt = datetime.now(tz=local_tz())
    from werkbank.settings_store import get_archive_language

    lang = get_archive_language()
    dt_str = format_datetime(dt, lang)
    badge = _BADGE_TEXT.get(lang, _BADGE_TEXT["en"])

    body_html = markdown2.markdown(
        content_markdown,
        extras=["fenced-code-blocks", "tables", "strike", "cuddled-lists", "header-ids",
                "link-patterns"],
        link_patterns=[(_URL_RE, r"\g<0>")],
    )
    body_html = _process_tables(body_html)

    title_escaped = html.escape(title)
    username_escaped = html.escape(username)
    model_escaped = html.escape(model_name)

    full_html = f"""<!DOCTYPE html>
<html><body>
<div class="doc-header">
  <div class="doc-title">{title_escaped}</div>
  <div class="meta-line">
    {dt_str} &nbsp;&bull;&nbsp; {username_escaped} &nbsp;&bull;&nbsp; {model_escaped}
    &nbsp;&nbsp;<span class="badge">{badge}</span>
  </div>
</div>
<hr class="header-rule"/>
{body_html}
</body></html>"""

    # WeasyPrint emits real PDF link annotations for every <a href>, so no
    # post-processing pass is needed (fitz.Story rendered links visually only).
    return HTML(string=full_html).write_pdf(stylesheets=[CSS(string=_USER_CSS)])

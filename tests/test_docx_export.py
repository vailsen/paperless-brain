"""Markdown → Word (`services/docx_export.py`)."""

import base64
import io

from docx import Document

from services.docx_export import filename, markdown_to_docx

MD = """# Result

The invoice over **1.240,00 €** is *due*.[^3-p2] See [the source](https://example.com/a) and `code`.

## List
- one
- two with **bold**
  - nested
1. first
2. second

| Item | Amount |
|---|---|
| Invoice | 1.240,00 € |

> A quote.

```
code block
line 2
```
"""


def _doc(md=MD, **kw):
    return Document(io.BytesIO(markdown_to_docx(md, "Report", username="alice", model_name="qwen", **kw)))


def test_structure_uses_words_own_styles():
    d = _doc()
    styles = [(p.style.name, p.text) for p in d.paragraphs if p.text]
    assert styles[0] == ("Title", "Report")
    assert ("Heading 1", "Result") in styles and ("Heading 2", "List") in styles
    assert ("List Bullet", "one") in styles and ("List Bullet 2", "nested") in styles
    assert ("List Number", "first") in styles
    assert ("Quote", "A quote.") in styles
    assert any(t == "code block\nline 2" for _s, t in styles)
    assert [[c.text for c in r.cells] for r in d.tables[0].rows] == [["Item", "Amount"], ["Invoice", "1.240,00 €"]]


def test_inline_formatting_links_and_citations():
    d = _doc()
    p = next(p for p in d.paragraphs if p.text.startswith("The invoice"))
    assert any(r.bold and "1.240,00" in r.text for r in p.runs)
    assert any(r.italic and r.text == "due" for r in p.runs)
    assert any(r.font.superscript and r.text == "1" for r in p.runs)  # [^3-p2] → ¹ …
    assert any(p.text.endswith("#3 · p. 2") for p in d.paragraphs)    # … and the source list
    links = [r.target_ref for r in d.part.rels.values() if r.reltype.endswith("/hyperlink")]
    assert links == ["https://example.com/a"]


def test_meta_line_and_german_labels():
    d = _doc(MD, lang="de")
    assert "KI-generiert" in d.paragraphs[1].text and "alice" in d.paragraphs[1].text
    assert any(p.text.endswith("#3 · S. 2") for p in d.paragraphs)


def test_pictures_inline_and_unresolved_ones_dropped():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    d = _doc(f"Before\n\n![x-ray]({uri})\n\n![gone](/api/doc-image/3/1)\n\nAfter")
    assert len(d.inline_shapes) == 1
    assert "gone" not in " ".join(p.text for p in d.paragraphs)


def test_empty_markdown_still_makes_a_document():
    assert _doc("").paragraphs[0].text == "Report"


def test_filename_is_safe():
    assert filename("Rechnung: März/2026 <final>") == "Rechnung_März2026_final.docx"
    assert filename("") == "document.docx"

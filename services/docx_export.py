"""Markdown → Word (.docx): the same content the PDF export renders, editable.

Everything that offers a PDF of generated text offers this too — a deep research
report, a research note, a finding — and the chat's answer and conversation
downloads. The input goes through the PDF's own preparation (`plain_citations`:
reference markers become superscript numbers and a source list; intraword
underscores escaped), then markdown2 → HTML → a walk that writes Word's own
styles: headings, paragraphs with bold / italic / code / links, bullet and
numbered lists (nested), tables, quotes, code blocks, pictures given inline as
data URIs. A Word user can restyle the document with the theme, which is the
point of not shipping a PDF.
"""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime

import markdown2
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from lxml import html as lxml_html

from services.citations import plain_citations
from services.markdown_text import escape_intraword_underscores

_URL_RE = re.compile(r'https?://[^\s<>"\[\]{}|\\^`]+[^\s<>"\[\]{}|\\^`.,;:!?)>]')
_UNRESOLVED_DOC_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(/api/doc-image/[^)\s]*\)")
_DATA_URI_RE = re.compile(r"data:image/[a-z+]+;base64,(.+)", re.S)
_BADGE = {"en": "AI-generated", "de": "KI-generiert"}
_MONO = "Consolas"
_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre", "hr", "div", "img"}

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def markdown_to_docx(content_markdown: str, title: str, *, username: str = "", model_name: str = "",
                     dt: datetime | None = None, lang: str = "en") -> bytes:
    """Render Markdown as a Word document and return its bytes."""
    from i18n import format_datetime

    content_markdown = _UNRESOLVED_DOC_IMAGE_RE.sub("", content_markdown or "")
    content_markdown = plain_citations(content_markdown, "S. {n}" if lang == "de" else "p. {n}")
    body_html = markdown2.markdown(
        escape_intraword_underscores(content_markdown),
        extras=["fenced-code-blocks", "tables", "strike", "cuddled-lists", "link-patterns"],
        link_patterns=[(_URL_RE, r"\g<0>")],
    )
    doc = Document()
    doc.core_properties.title = title or ""
    if username:
        doc.core_properties.author = username
    if title:
        doc.add_heading(title, level=0)
    meta = [x for x in (format_datetime(dt, lang) if dt else "", username, model_name,
                        _BADGE.get(lang, _BADGE["en"])) if x]
    if meta:
        p = doc.add_paragraph()
        r = p.add_run("  ·  ".join(meta))
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
    root = lxml_html.fragment_fromstring(body_html, create_parent="div")
    _Writer(doc).blocks(root)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class _Writer:
    def __init__(self, doc) -> None:
        self.doc = doc

    # ── Blocks ───────────────────────────────────────────────────────────────
    def blocks(self, el, *, quote: bool = False) -> None:
        loose: list = []  # inline content directly in a container (text, <a>, …)

        def flush() -> None:
            if loose and any((isinstance(x, str) and x.strip()) or not isinstance(x, str) for x in loose):
                p = self.doc.add_paragraph(style="Quote" if quote else None)
                for x in loose:
                    if isinstance(x, str):
                        self._text(p, x, {})
                    else:
                        self.inline(p, x, {})
            loose.clear()

        if el.text:
            loose.append(el.text)
        for ch in el:
            tag = ch.tag if isinstance(ch.tag, str) else ""
            if tag in _BLOCKS:
                flush()
                self.block(ch, quote=quote)
            else:
                loose.append(ch)
            if ch.tail:
                loose.append(ch.tail)
        flush()

    def block(self, el, *, quote: bool = False) -> None:
        tag = el.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            p = self.doc.add_heading(level=min(int(tag[1]), 4))
            self.inline_children(p, el, {})
        elif tag == "p" and any(ch.tag == "img" for ch in el):
            # markdown2 puts a picture in a paragraph: the picture on its own
            # line, the words around it in paragraphs of their own.
            p = self.doc.add_paragraph(style="Quote" if quote else None)
            if el.text:
                self._text(p, el.text, {})
            for ch in el:
                if ch.tag == "img":
                    self._image(ch)
                    p = self.doc.add_paragraph(style="Quote" if quote else None)
                else:
                    self.inline(p, ch, {})
                if ch.tail:
                    self._text(p, ch.tail, {})
        elif tag == "p":
            p = self.doc.add_paragraph(style="Quote" if quote else None)
            self.inline_children(p, el, {})
        elif tag in ("ul", "ol"):
            self.list(el, tag == "ol", 1)
        elif tag == "table":
            self.table(el)
        elif tag == "blockquote":
            self.blocks(el, quote=True)
        elif tag == "pre":
            p = self.doc.add_paragraph()
            r = p.add_run(el.text_content().rstrip("\n"))
            r.font.name = _MONO
            r.font.size = Pt(9)
        elif tag == "hr":
            self._rule(self.doc.add_paragraph())
        elif tag == "img":
            self._image(el)
        else:  # div
            self.blocks(el, quote=quote)

    def list(self, el, ordered: bool, level: int) -> None:
        style = ("List Number" if ordered else "List Bullet") + (f" {level}" if level > 1 else "")
        for li in el:
            if li.tag != "li":
                continue
            p = self.doc.add_paragraph(style=style)
            nested = []
            if li.text:
                self._text(p, li.text, {})
            for ch in li:
                if ch.tag in ("ul", "ol"):
                    nested.append(ch)
                elif ch.tag == "p":  # a loose list: its paragraphs run into one item
                    self.inline_children(p, ch, {})
                else:
                    self.inline(p, ch, {})
                if ch.tail and ch.tail.strip():
                    self._text(p, ch.tail, {})
            for n in nested:
                self.list(n, n.tag == "ol", min(level + 1, 3))

    def table(self, el) -> None:
        rows = [tr for tr in el.iter("tr")]
        if not rows:
            return
        ncols = max(len([c for c in tr if c.tag in ("td", "th")]) for tr in rows)
        if not ncols:
            return
        t = self.doc.add_table(rows=len(rows), cols=ncols)
        t.style = "Table Grid"
        for i, tr in enumerate(rows):
            cells = [c for c in tr if c.tag in ("td", "th")]
            for j, c in enumerate(cells):
                p = t.cell(i, j).paragraphs[0]
                self.inline_children(p, c, {"b": c.tag == "th"})
        self.doc.add_paragraph()

    # ── Inline ───────────────────────────────────────────────────────────────
    def inline_children(self, p, el, fmt: dict) -> None:
        if el.text:
            self._text(p, el.text, fmt)
        for ch in el:
            self.inline(p, ch, fmt)
            if ch.tail:
                self._text(p, ch.tail, fmt)

    def inline(self, p, el, fmt: dict) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("strong", "b"):
            self.inline_children(p, el, {**fmt, "b": True})
        elif tag in ("em", "i"):
            self.inline_children(p, el, {**fmt, "i": True})
        elif tag in ("del", "s", "strike"):
            self.inline_children(p, el, {**fmt, "strike": True})
        elif tag == "code":
            self.inline_children(p, el, {**fmt, "code": True})
        elif tag == "sup":
            self.inline_children(p, el, {**fmt, "sup": True})
        elif tag == "sub":
            self.inline_children(p, el, {**fmt, "sub": True})
        elif tag == "br":
            p.add_run().add_break()
        elif tag == "a":
            href = el.get("href") or ""
            if href.startswith(("http://", "https://", "mailto:")):
                self._link(p, href, el.text_content(), fmt)
            else:
                self.inline_children(p, el, fmt)
        elif tag == "img":
            self._text(p, el.get("alt") or "", {**fmt, "i": True})
        elif tag in _BLOCKS:  # a block where markdown2 put one inline (rare): keep its words
            self._text(p, " " + el.text_content() + " ", fmt)
        else:
            self.inline_children(p, el, fmt)

    def _text(self, p, text: str, fmt: dict) -> None:
        text = re.sub(r"\s+", " ", text)
        if not text:
            return
        r = p.add_run(text)
        self._fmt(r, fmt)

    @staticmethod
    def _fmt(r, fmt: dict) -> None:
        r.bold = fmt.get("b") or None
        r.italic = fmt.get("i") or None
        if fmt.get("strike"):
            r.font.strike = True
        if fmt.get("sup"):
            r.font.superscript = True
        if fmt.get("sub"):
            r.font.subscript = True
        if fmt.get("code"):
            r.font.name = _MONO

    def _link(self, p, url: str, text: str, fmt: dict) -> None:
        """A real hyperlink — python-docx has no API for one."""
        rid = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
        h = OxmlElement("w:hyperlink")
        h.set(qn("r:id"), rid)
        run = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "Hyperlink")
        rpr.append(style)
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "2A55C4")
        rpr.append(color)
        u = OxmlElement("w:u")
        u.set(qn("w:val"), "single")
        rpr.append(u)
        if fmt.get("b"):
            rpr.append(OxmlElement("w:b"))
        run.append(rpr)
        t = OxmlElement("w:t")
        t.text = re.sub(r"\s+", " ", text) or url
        t.set(qn("xml:space"), "preserve")
        run.append(t)
        h.append(run)
        p._p.append(h)

    def _image(self, el) -> None:
        m = _DATA_URI_RE.match(el.get("src") or "")
        if not m:
            if el.get("alt"):
                self.doc.add_paragraph().add_run(el.get("alt")).italic = True
            return
        from docx.shared import Cm

        try:
            self.doc.add_picture(io.BytesIO(base64.b64decode(m.group(1))), width=Cm(14))
            self.doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        except Exception:  # noqa: BLE001 — a picture Word cannot take: its caption stays
            if el.get("alt"):
                self.doc.add_paragraph().add_run(el.get("alt")).italic = True

    @staticmethod
    def _rule(p) -> None:
        ppr = p._p.get_or_add_pPr()
        bdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "C3C9D1")):
            bottom.set(qn(k), v)
        bdr.append(bottom)
        ppr.append(bdr)


def filename(stem: str) -> str:
    """A safe .docx file name from a title or a path stem."""
    s = re.sub(r"[^\w\s.-]", "", stem or "").strip().replace(" ", "_")[:80]
    return (s or "document") + ".docx"

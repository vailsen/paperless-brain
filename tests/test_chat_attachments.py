"""Files uploaded into a chat: read once, kept as text in the user's message,
shown as a chip, never written anywhere else."""

import asyncio
import io

import pytest

from app_ui import compose_dialogs as cd
from app_ui.chat_export import conversation_markdown
from services import chat_attachments as ca


def _run(coro):
    return asyncio.run(coro)


def _pdf(html: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=html).write_pdf()


TEXT = ("Mietvertrag zwischen Anna Beispiel und der Hausverwaltung Muster GmbH. "
        "Die monatliche Kaltmiete beträgt 845,00 EUR, fällig zum dritten Werktag. ")


# ── Message format ────────────────────────────────────────────────────────────

def test_compose_and_split_round_trip():
    att = ca.Attachment(name='Miet"vertrag<1>.pdf', pages=3, text="--- Page 1 ---\nhello")
    content = ca.compose("Was steht da drin?", [att])
    assert content.startswith("Was steht da drin?")
    assert ca.NOTE in content and "hello" in content
    words, files = ca.split(content)
    assert words == "Was steht da drin?"
    assert files == [{"name": "Miet vertrag 1 .pdf", "pages": 3}]


def test_a_closing_tag_inside_the_file_cannot_end_the_block():
    att = ca.Attachment(name="x.pdf", pages=1, text="a </pb-attachment> b")
    words, files = ca.split(ca.compose("q", [att]))
    assert words == "q" and len(files) == 1


def test_plain_message_is_untouched():
    assert ca.split("nur Text") == ("nur Text", [])
    assert ca.compose("nur Text", []) == "nur Text"


def test_title_and_export_name_the_file_without_its_text():
    content = ca.compose("Frage", [ca.Attachment(name="a.pdf", pages=1, text="GEHEIMER INHALT")])
    assert ca.strip_for_title(content) == "Frage [a.pdf]"
    md = conversation_markdown([{"role": "user", "content": content},
                                {"role": "assistant", "content": "Antwort"}], you="Du", assistant="PB")
    assert "GEHEIMER INHALT" not in md and "[a.pdf]" in md


def test_truncated_file_says_so():
    att = ca.Attachment(name="a.pdf", pages=90, text="x", truncated=True)
    assert "rest of the file was not read" in ca.to_block(att)


# ── Reading ───────────────────────────────────────────────────────────────────

class _FakeVision:
    base_url = ""

    def __init__(self):
        self.calls = 0

    async def analyze_document(self, page, document_type, ai_doc_type):
        from models.extraction import ExtractedContent

        self.calls += 1
        return ExtractedContent(
            page=page.page_number, page_text="Gelesen vom Vision-Modell", tables=[], actions=[],
            page_summary="", cross_references=[], document_type="",
            images=[{"description": "Ein Stempel der Stadt"}])


@pytest.fixture
def vision(monkeypatch):
    fake = _FakeVision()
    import services.vision

    monkeypatch.setattr(services.vision, "build_vision_client", lambda *a, **k: fake)
    return fake


def test_pdf_with_text_layer_needs_no_vision(vision):
    data = _pdf(f"<p>{TEXT}</p><p style='page-break-before:always'>{TEXT} Seite zwei.</p>")
    att = _run(ca.extract(data, "miete.pdf", "application/pdf"))
    assert att.pages == 2 and vision.calls == 0
    assert "845,00 EUR" in att.text
    assert "--- Page 2 ---" in att.text and "Seite zwei" in att.text


def test_scanned_page_goes_to_the_vision_model(vision):
    data = _pdf("<div style='height:100px;background:#000'></div>")  # no text at all
    att = _run(ca.extract(data, "scan.pdf", "application/pdf"))
    assert vision.calls == 1 and att.vision_pages == 1
    assert "Gelesen vom Vision-Modell" in att.text
    assert "[Image 1: Ein Stempel der Stadt]" in att.text


def test_picture_goes_to_the_vision_model(vision):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(buf, format="PNG")
    progress = []
    att = _run(ca.extract(buf.getvalue(), "foto.png", "image/png",
                          on_progress=lambda d, t: progress.append((d, t))))
    assert att.pages == 1 and vision.calls == 1
    assert progress[-1] == (1, 1)
    assert "--- Page" not in att.text  # one picture, no page headers


def test_long_pdf_is_cut_at_max_pages(vision, monkeypatch):
    monkeypatch.setattr(ca, "MAX_PAGES", 2)
    pages = "".join(f"<p style='page-break-before:always'>{TEXT} {i}</p>" for i in range(4))
    att = _run(ca.extract(_pdf(pages), "lang.pdf", "application/pdf"))
    assert att.pages == 4 and att.truncated
    assert att.text.count("--- Page") == 2


@pytest.mark.parametrize("data,name,ctype", [
    (b"", "a.pdf", "application/pdf"),
    (b"hello", "a.txt", "text/plain"),
    (b"%PDF-1.4 broken", "a.pdf", "application/pdf"),
    (b"not an image", "a.png", "image/png"),
])
def test_unreadable_files_raise_a_short_reason(vision, data, name, ctype):
    with pytest.raises(ca.AttachmentError):
        _run(ca.extract(data, name, ctype))


def test_too_large_is_refused_before_reading(vision, monkeypatch):
    monkeypatch.setattr(ca, "MAX_BYTES", 10)
    with pytest.raises(ca.AttachmentError):
        _run(ca.extract(b"%PDF-" + b"x" * 20, "a.pdf", "application/pdf"))
    assert vision.calls == 0


# ── Letter / email draft ─────────────────────────────────────────────────────

def test_draft_request_carries_conversation_instruction_and_date():
    msgs = [{"role": "user", "content": ca.compose("Widerspruch?", [
        ca.Attachment(name="bescheid.pdf", pages=1, text="Aktenzeichen 12/345")])},
            {"role": "assistant", "content": "Die Frist endet am 01.12.2026."}]
    system, request = cd.build_draft_request("letter", msgs, "Einspruch einlegen")
    assert "DIN 5008" in system and "never invent" in system
    assert "Aktenzeichen 12/345" in request          # the file's content is there for the draft
    assert "Die Frist endet am 01.12.2026." in request
    assert "Einspruch einlegen" in request


def test_excerpt_keeps_the_newest_part():
    msgs = [{"role": "user", "content": "alt " * 5000}, {"role": "assistant", "content": "NEU"}]
    ex = cd.conversation_excerpt(msgs, limit=200)
    assert ex.endswith("Assistant: NEU") and len(ex) <= 210


def test_parse_draft_accepts_fenced_json_and_drops_unknown_keys():
    raw = '<think>hm</think>```json\n{"subject": "Einspruch", "body_paras": "A\\n\\nB", ' \
          '"recipient_name": "Finanzamt", "evil": "x"}\n```'
    d = cd.parse_draft(raw, "letter")
    assert d == {"subject": "Einspruch", "body_paras": ["A", "B"], "recipient_name": "Finanzamt"}


def test_parse_draft_garbage_is_an_empty_draft():
    assert cd.parse_draft("Sorry, I can't", "email") == {}
    assert cd.parse_draft("{not json}", "email") == {}


def test_mailto_url_encodes_subject_and_body():
    url = cd.mailto_url("a@b.de", "Frage & Antwort", "Zeile 1\nZeile 2")
    assert url.startswith("mailto:a@b.de?")
    assert "Frage%20%26%20Antwort" in url and "Zeile%201%0AZeile%202" in url


# ── Email conversation ───────────────────────────────────────────────────────


def test_email_chat_knows_the_conversation_and_the_draft_form():
    system = cd.build_email_chat_system([{"role": "user", "content": "Kündigung Vodafone?"},
                                         {"role": "assistant", "content": "Frist: 3 Monate."}])
    assert "Kündigung Vodafone?" in system and "Frist: 3 Monate." in system
    assert "<email>" in system and "{conversation}" not in system and "{today}" not in system


def test_a_question_back_is_no_draft():
    reply = "Wem soll die Mail gehen — dem Vermieter oder der Hausverwaltung?"
    assert cd.email_draft(reply) is None
    assert cd.visible_reply(reply) == reply


def test_the_draft_block_becomes_the_preview_and_leaves_the_bubble():
    reply = ('Hier ist der Entwurf.\n<email>{"to": "info@example.org", "subject": "Zählerstände",'
             ' "body": "Sehr geehrte Damen und Herren,\n\nbitte …\n\nMit freundlichen Grüßen"}</email>')
    d = cd.email_draft(reply)
    assert d == {"to": "info@example.org", "subject": "Zählerstände",
                 "body": "Sehr geehrte Damen und Herren,\n\nbitte …\n\nMit freundlichen Grüßen"}
    assert cd.visible_reply(reply) == "Hier ist der Entwurf."


def test_draft_tolerates_fences_thinking_and_a_missing_close_tag():
    reply = '<think>hmm</think>Ok.\n<email>```json\n{"subject": "S", "body_paras": ["A", "B"]}\n```'
    assert cd.email_draft(reply) == {"to": "", "subject": "S", "body": "A\n\nB"}
    assert cd.visible_reply(reply) == "Ok."


def test_streaming_hides_a_half_arrived_tag():
    assert cd.visible_reply("Hier der Entwurf.\n<ema") == "Hier der Entwurf."
    assert cd.email_draft('<email>{"to": ""}</email>') is None   # nothing to show

"""Letter (DIN 5008, DOCX) and email draft — buttons in the chat's "…" menu.

They used to be model tools (`trigger_docx_generation`, `create_email`): two
schemas, ~3 000 characters, read by the model on every call of every turn so
that it could, once in a while, open a dialog. A letter is something the user
explicitly asks for, so the user opens it (2026-11-15). The draft is one model
call without tools, made only when the dialog opens, from the conversation the
user is looking at — the dialog shows it in editable fields, nothing is sent
or saved without the user.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import date

from nicegui import ui

_log = logging.getLogger("compose")

#: The tail of the conversation the draft is written from.
DRAFT_CONTEXT_CHARS = 12_000

LETTER_KEYS = (
    "recipient_name", "recipient_street", "recipient_postcode", "recipient_city",
    "subject", "salutation", "body_paras", "source_cross_ref", "src_doc_info",
)
EMAIL_KEYS = ("recipient_email", "subject", "salutation", "body_paras")

_RULES = (
    "Use only facts that appear in the conversation — names, addresses, numbers, dates, "
    "references. A field the conversation does not give stays an empty string; never invent "
    "an address, an email address or a reference. Write the text in the language of the "
    "conversation; a German letter keeps German conventions (salutation 'Sehr geehrte Damen und "
    "Herren,'). body_paras is a list of paragraphs without salutation and without closing. "
    "Answer with the JSON object only."
)

_SYSTEM = {
    "letter": (
        "You draft a formal business letter (DIN 5008) from a conversation between a user and "
        "their document assistant. Return a JSON object with the keys recipient_name, "
        "recipient_street, recipient_postcode, recipient_city, subject, salutation, body_paras, "
        "source_cross_ref (the recipient's reference with its prefix, e.g. 'Rechn. Nr. 260412'), "
        "src_doc_info (short title of the document the letter refers to). " + _RULES
    ),
    "email": (
        "You draft an email from a conversation between a user and their document assistant. "
        "Return a JSON object with the keys recipient_email, subject, salutation, body_paras "
        "(the last paragraph carries the closing). " + _RULES
    ),
}


def conversation_excerpt(messages: list[dict], limit: int = DRAFT_CONTEXT_CHARS) -> str:
    """The newest part of the conversation that fits, oldest first."""
    from services.chat_attachments import strip_for_title

    out: list[str] = []
    used = 0
    for m in reversed(messages):
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if m.get("role") == "user":
            content = strip_for_title(content, keep_attachment_text=True)
        line = f"{'User' if m.get('role') == 'user' else 'Assistant'}: {content.strip()}"
        if used + len(line) > limit:
            out.append(line[-(limit - used):])
            break
        out.append(line)
        used += len(line)
    return "\n\n".join(reversed(out))


def build_draft_request(kind: str, messages: list[dict], instruction: str = "") -> tuple[str, str]:
    """(system, user message) for the one draft call."""
    request = f"Conversation:\n\n{conversation_excerpt(messages)}"
    if instruction.strip():
        request += f"\n\nWhat the user wants the {kind} to say: {instruction.strip()}"
    request += f"\n\nToday is {date.today().isoformat()}. Draft the {kind} now."
    return _SYSTEM[kind], request


def parse_draft(raw: str, kind: str) -> dict:
    """The model's JSON, reduced to the known keys with the right types.
    Anything unparseable is an empty draft — the user fills the fields."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for key in LETTER_KEYS if kind == "letter" else EMAIL_KEYS:
        value = data.get(key)
        if key == "body_paras":
            if isinstance(value, str):
                value = [p for p in re.split(r"\n\s*\n", value) if p.strip()]
            out[key] = [str(p).strip() for p in value or [] if str(p).strip()] if isinstance(value, list) else []
        elif value not in (None, ""):
            out[key] = str(value).strip()
    return out


DraftFn = Callable[[str, str], Awaitable[dict]]


def _section(label: str) -> None:
    ui.label(label).classes("text-xs text-gray-400 mt-3 mb-1 font-semibold tracking-wide")


def _draft_bar(_, kind: str, draft_fn: DraftFn | None, apply: Callable[[dict], None]):
    """Instruction field + "Draft from the chat" + status. Drafts once on open."""
    if draft_fn is None:
        return None
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        instruction = ui.input(
            _("What should it say? (optional)")
        ).props("outlined dark dense").classes("flex-1")
        btn = ui.button(_("Draft from the chat"), icon="auto_awesome").props(
            "flat dark dense no-caps").classes("text-gray-300")
    status = ui.label("").classes("text-xs text-gray-500")

    async def _run() -> None:
        btn.disable()
        status.set_text(_("Drafting from the conversation …"))
        try:
            draft = await draft_fn(kind, instruction.value or "")
        except Exception as exc:  # noqa: BLE001 — shown, never fatal
            _log.warning("compose draft failed", exc_info=True)
            status.set_text(_("The draft failed: {err}").format(err=exc))
        else:
            if draft:
                apply(draft)
                status.set_text(_("Draft from the conversation — check every field."))
            else:
                status.set_text(_("The model returned no usable draft — fill in the fields."))
        finally:
            btn.enable()

    btn.on_click(_run)
    return _run


def open_letter_dialog(host, _, *, sender: dict, draft_fn: DraftFn | None) -> Callable | None:
    """Build and open the letter dialog under `host`. Returns the drafting
    coroutine (or None) so the caller can start the first draft."""
    host.clear()
    with host, ui.dialog().props("persistent") as dlg, ui.card().style(
        "background:var(--c-surface); width:min(560px, 95vw); max-height:90vh; overflow-y:auto;"
    ):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label(_("Write a letter")).classes("text-base font-semibold text-gray-100")
            ui.button(icon="close", on_click=dlg.close).props("flat dark dense").classes("text-gray-400")

        fields: dict[str, ui.input] = {}

        def _inp(key: str, label: str, value: str = "", **style) -> ui.input:
            el = ui.input(label, value=value).props("outlined dark dense")
            el.classes("w-full") if not style else el.style(style["style"])
            fields[key] = el
            return el

        def _apply(d: dict) -> None:
            for key, el in fields.items():
                if key == "body_paras":
                    if d.get("body_paras"):
                        el.set_value("\n\n".join(d["body_paras"]))
                elif d.get(key):
                    el.set_value(d[key])

        run = _draft_bar(_, "letter", draft_fn, _apply)

        _section(_("Sender"))
        _inp("s_name", _("Name"), sender.get("name", ""))
        _inp("s_company", _("Company"), sender.get("company", ""))
        _inp("s_street", _("Street"), sender.get("street", ""))
        with ui.row().classes("w-full gap-2 no-wrap"):
            _inp("s_plz", _("Postcode"), sender.get("plz", ""), style="width:30%")
            _inp("s_city", _("City"), sender.get("city", ""), style="flex:1")
        _inp("s_phone", _("Phone"), sender.get("phone", ""))
        _inp("s_email", _("Email"), sender.get("email", ""))

        _section(_("Recipient"))
        _inp("recipient_name", _("Name"))
        _inp("recipient_street", _("Street"))
        with ui.row().classes("w-full gap-2 no-wrap"):
            _inp("recipient_postcode", _("Postcode"), style="width:30%")
            _inp("recipient_city", _("City"), style="flex:1")

        _section(_("Content"))
        _inp("source_cross_ref", _("Your reference (invoice no. / file no.)"))
        _inp("src_doc_info", _("Reference (short title of ref. doc., empty = none)"))
        _inp("subject", _("Subject"))
        _inp("salutation", _("Salutation"), "Sehr geehrte Damen und Herren,")
        fields["body_paras"] = ui.textarea(
            _("Letter body (separate paragraphs with blank lines)")
        ).props("outlined dark dense rows=8").classes("w-full")
        _inp("closing", _("Closing"), sender.get("closing", "Mit freundlichen Grüßen"))

        def _download() -> None:
            from services.docx_service import generate_letter_docx

            v = {k: (el.value or "").strip() for k, el in fields.items()}
            try:
                data = generate_letter_docx(
                    sender={"name": v["s_name"], "company": v["s_company"], "street": v["s_street"],
                            "plz": v["s_plz"], "city": v["s_city"], "phone": v["s_phone"],
                            "email": v["s_email"]},
                    recipient={"name": v["recipient_name"], "street": v["recipient_street"],
                               "postcode": v["recipient_postcode"], "city": v["recipient_city"]},
                    subject=v["subject"],
                    salutation=v["salutation"],
                    body_paras=[p.strip() for p in (fields["body_paras"].value or "").split("\n\n") if p.strip()],
                    closing=v["closing"] or "Mit freundlichen Grüßen",
                    source_cross_ref=v["source_cross_ref"],
                    src_doc_info=v["src_doc_info"],
                    reference_doc_id=None,
                )
            except Exception as exc:  # noqa: BLE001
                ui.notify(_("Error: {err}").format(err=exc), type="negative")
                return
            subj = re.sub(r"[^\wÀ-ſ]+", "_", v["subject"])[:28].strip("_")
            who = re.sub(r"[^\wÀ-ſ]", "", (v["recipient_name"].split() or ["Empfaenger"])[0])
            ui.download(
                data, f"{date.today():%y_%m_%d}_Brief_{subj}_{who}.docx",
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            ui.button(_("Cancel"), on_click=dlg.close).props("flat dark dense").classes("text-gray-400")
            ui.button(_("Download letter"), icon="download", on_click=_download).props(
                "unelevated dark").classes("bg-primary text-white")
    dlg.open()
    return run


def mailto_url(to: str, subject: str, body: str) -> str:
    return (
        "mailto:" + urllib.parse.quote(to.strip(), safe="@,")
        + "?" + urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)
    )


def open_email_dialog(host, _, *, draft_fn: DraftFn | None) -> Callable | None:
    host.clear()
    with host, ui.dialog().props("persistent") as dlg, ui.card().style(
        "background:var(--c-surface); width:min(520px, 95vw); max-height:90vh; overflow-y:auto;"
    ):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label(_("Write an email")).classes("text-base font-semibold text-gray-100")
            ui.button(icon="close", on_click=dlg.close).props("flat dark dense").classes("text-gray-400")

        to = ui.input(_("To")).props("outlined dark dense").classes("w-full")
        subject = ui.input(_("Subject")).props("outlined dark dense").classes("w-full")
        body = ui.textarea(_("Message")).props("outlined dark dense rows=9").classes("w-full")

        def _apply(d: dict) -> None:
            if d.get("recipient_email"):
                to.set_value(d["recipient_email"])
            if d.get("subject"):
                subject.set_value(d["subject"])
            paras = ([d["salutation"]] if d.get("salutation") else []) + list(d.get("body_paras") or [])
            if paras:
                body.set_value("\n\n".join(paras))

        # Above the fields, as in the letter dialog.
        with ui.element("div").classes("w-full") as bar:
            run = _draft_bar(_, "email", draft_fn, _apply)
        bar.move(target_index=1)

        async def _copy(value: str, msg: str) -> None:
            await ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(value)})")
            ui.notify(msg, timeout=1200)

        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            ui.button(_("Copy message"), icon="content_copy",
                      on_click=lambda: _copy(body.value or "", _("Message copied!"))).props(
                "flat dark dense").classes("text-gray-400")
            ui.button(
                _("Open in mail app"), icon="open_in_new",
                on_click=lambda: ui.run_javascript(
                    f"window.location.href = {json.dumps(mailto_url(to.value or '', subject.value or '', body.value or ''))}"),
            ).props("unelevated dark").classes("bg-primary text-white")
    dlg.open()
    return run

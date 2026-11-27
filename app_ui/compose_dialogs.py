"""Letter (DIN 5008, DOCX) and email — buttons in the chat's "…" menu.

They used to be model tools (`trigger_docx_generation`, `create_email`): two
schemas, ~3 000 characters, read by the model on every call of every turn so
that it could, once in a while, open a dialog. A letter is something the user
explicitly asks for, so the user opens it (2026-11-15). The draft is one model
call without tools, made only when the dialog opens, from the conversation the
user is looking at — the dialog shows it in editable fields, nothing is sent
or saved without the user.

The email is a small conversation of its own (2026-11-25): the user says what
the mail is about, the model may ask back (recipient, purpose, facts), and the
model decides when the draft is ready — it then answers with an
`<email>{…}</email>` block, which the dialog turns into the preview (editable,
copy, open in the mail app). The chat the user came from is in its system
prompt as context. Still no tools, and nothing leaves the dialog by itself.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
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
#: (system, messages) → the reply, streamed as text pieces.
ChatFn = Callable[[str, list[dict]], AsyncIterator[str]]

_EMAIL_CHAT_SYSTEM = """You help the user write an email. You are part of their document \
assistant; the conversation they just had with it is below, as context.

How to work:
- When you know what the email should say, write it right away. Otherwise ask: one short \
question per reply, only what you really need (recipient, purpose, the key facts, tone). The \
user may also simply tell you.
- Use only facts from the conversation or from the user. Never invent an email address, a \
name, a number, a date or a reference. An address you do not know stays empty.
- Answer in the language the user writes to you in. The email itself is in that language \
too, unless the user asks for another.
- When the draft is ready for the user to check, end your reply with it in exactly this \
form, and write at most one short sentence before it:
<email>{"to": "", "subject": "", "body": ""}</email>
  `body` is the whole text with salutation and closing, paragraphs separated by \\n\\n.
- When the user asks for changes, answer with the complete revised draft in the same form.

Today is {today}.

Conversation (context):
{conversation}"""

_EMAIL_BLOCK_RE = re.compile(r"<email>(.*?)(?:</email>|$)", re.S | re.I)


def build_email_chat_system(messages: list[dict]) -> str:
    """The system prompt of the email conversation: the rules, then the chat
    the user came from (its newest part)."""
    conv = conversation_excerpt(messages) or "(none — the user starts from scratch)"
    return _EMAIL_CHAT_SYSTEM.replace("{today}", date.today().isoformat()).replace("{conversation}", conv)


def visible_reply(text: str) -> str:
    """What the bubble shows of a (partial) reply: no thinking, no draft
    block, no half-arrived "<ema" at the end."""
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text or "", flags=re.S)
    cut = text.lower().find("<email")
    if cut >= 0:
        text = text[:cut]
    return re.sub(r"<[a-zA-Z]{0,5}$", "", text).strip()


def email_draft(text: str) -> dict | None:
    """The draft of a finished reply — ``{to, subject, body}`` — or None when
    the model is still asking."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = _EMAIL_BLOCK_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1], strict=False)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    body = data.get("body")
    if body is None and isinstance(data.get("body_paras"), list):
        body = "\n\n".join(str(p) for p in data["body_paras"])
    out = {
        "to": str(data.get("to") or data.get("recipient_email") or "").strip(),
        "subject": str(data.get("subject") or "").strip(),
        "body": str(body or "").strip(),
    }
    return out if out["subject"] or out["body"] else None


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


def open_email_dialog(host, _, *, chat_fn: ChatFn | None, context: list[dict]) -> None:
    """The email conversation: chat with the model until it shows a draft,
    then the preview. Back to the conversation for changes at any time."""
    host.clear()
    system = build_email_chat_system(context)
    history: list[dict] = []          # what the model sees
    st = {"draft": None, "busy": False}
    with host, ui.dialog().props("persistent") as dlg, ui.card().classes("pb-mail gap-2").style(
        "background:var(--c-surface); width:min(560px, 95vw); height:min(720px, 88vh);"
        "display:flex; flex-direction:column; flex-wrap:nowrap; padding:12px;"
    ):
        ui.add_css(
            ".pb-mail-log{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;display:flex;"
            "flex-direction:column;gap:8px;padding:2px}"
            ".pb-mail-msg{max-width:88%;min-width:0;padding:6px 10px;border-radius:10px;font-size:.9rem;"
            "overflow-wrap:anywhere}"
            ".pb-mail-msg.is-user{align-self:flex-end;background:var(--c-accent-soft);color:var(--c-text)}"
            ".pb-mail-msg.is-model{align-self:flex-start;background:var(--c-surface-2, rgba(127,127,127,.12));"
            "color:var(--c-text)}"
            ".pb-mail-msg p{margin:0 0 .4em}.pb-mail-msg p:last-child{margin:0}"
        )
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label(_("Write an email")).classes("text-base font-semibold").style("color:var(--c-text)")
            ui.button(icon="close", on_click=dlg.close).props("flat dense round").classes("card-action-btn")

        # ── conversation ─────────────────────────────────────────────────────
        talk = ui.column().classes("w-full gap-2 no-wrap").style("flex:1;min-height:0;")
        with talk:
            log = ui.element("div").classes("pb-mail-log w-full")
            with log:
                with ui.element("div").classes("pb-mail-msg is-model"):
                    ui.markdown(_(
                        "What should the email be about, and to whom? I know the conversation you "
                        "came from. I'll ask if something is missing."
                    ))
            if chat_fn is None:
                ui.label(_("No model selected")).classes("text-xs").style("color:var(--c-warn)")
            with ui.row().classes("w-full items-end gap-2 no-wrap"):
                box = ui.textarea(placeholder=_("e.g. To the landlord: ask for the meter readings")).props(
                    "outlined dense autogrow rows=1").classes("flex-1 min-w-0")
                send = ui.button(icon="send").props("flat dense round color=primary")
                to_preview = ui.button(icon="preview").props("flat dense round").classes("card-action-btn")
                to_preview.tooltip(_("Show the draft"))
                to_preview.set_visibility(False)

        # ── preview ──────────────────────────────────────────────────────────
        prev = ui.column().classes("w-full gap-2 no-wrap").style("flex:1;min-height:0;overflow-y:auto;")
        with prev:
            ui.label(_("Check the draft — every field can be edited.")).classes("text-xs").style(
                "color:var(--c-text-muted)")
            to = ui.input(_("To")).props("outlined dense").classes("w-full")
            subject = ui.input(_("Subject")).props("outlined dense").classes("w-full")
            body = ui.textarea(_("Message")).props("outlined dense autogrow").classes("w-full")

            async def _copy(value: str, msg: str) -> None:
                await ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(value)})")
                ui.notify(msg, timeout=1200)

            with ui.row().classes("w-full justify-between items-center gap-2 flex-wrap"):
                ui.button(_("Ask for changes"), icon="chat",
                          on_click=lambda: _show(False)).props("flat dense no-caps")
                with ui.row().classes("gap-2 flex-wrap justify-end"):
                    ui.button(_("Copy message"), icon="content_copy",
                              on_click=lambda: _copy(body.value or "", _("Message copied!"))).props(
                        "flat dense no-caps")
                    ui.button(
                        _("Open in mail app"), icon="open_in_new",
                        on_click=lambda: ui.run_javascript(
                            "window.location.href = "
                            + json.dumps(mailto_url(to.value or "", subject.value or "", body.value or ""))),
                    ).props("unelevated dense no-caps color=primary")
        prev.set_visibility(False)

        def _show(preview: bool) -> None:
            prev.set_visibility(preview)
            talk.set_visibility(not preview)
            to_preview.set_visibility(st["draft"] is not None)

        def _bubble(role: str, text: str = ""):
            with log:
                el = ui.element("div").classes(f"pb-mail-msg is-{role}")
                with el:
                    md = ui.markdown(text)
            ui.run_javascript(
                f"(function(){{var l=document.getElementById('{log.html_id}');if(l)l.scrollTop=l.scrollHeight}})()")
            return md

        async def _send() -> None:
            text = (box.value or "").strip()
            if not text or st["busy"] or chat_fn is None:
                return
            box.set_value("")
            _bubble("user", text)
            content = text
            d = st["draft"]
            if d is not None:
                now = {"to": to.value or "", "subject": subject.value or "", "body": body.value or ""}
                if now != d:
                    # The user edited the preview: the model revises that, not its own version.
                    content += "\n\n(The draft as I edited it:\n" + json.dumps(now, ensure_ascii=False) + ")"
            history.append({"role": "user", "content": content})
            st["busy"] = True
            send.disable()
            md = _bubble("model", "…")
            parts: list[str] = []
            try:
                async for piece in chat_fn(system, list(history)):
                    parts.append(piece)
                    md.set_content(visible_reply("".join(parts)) or "…")
            except Exception as exc:  # noqa: BLE001 — shown, never fatal
                _log.warning("email chat failed", exc_info=True)
                md.set_content(_("The model did not answer: {err}").format(err=exc))
                history.pop()
                return
            finally:
                st["busy"] = False
                send.enable()
            reply = "".join(parts)
            history.append({"role": "assistant", "content": reply})
            draft = email_draft(reply)
            shown = visible_reply(reply)
            if draft is None:
                md.set_content(shown or _("(no answer)"))
                return
            md.set_content(shown or _("Here is the draft."))
            st["draft"] = draft
            to.set_value(draft["to"])
            subject.set_value(draft["subject"])
            body.set_value(draft["body"])
            _show(True)

        send.on_click(_send)
        to_preview.on_click(lambda: _show(True))
        box.on("keydown.enter.exact.prevent", _send)
    dlg.open()

"""Pictures from ``show_document_image`` as part of a chat answer.

The tool only emits an event; this turns the events of one turn into Markdown
appended to the answer. Markdown rather than HTML because the result becomes the
stored message: it is replayed to the model on the next turn, where an image
line reads as what it is, and it re-renders unchanged after a reload.
"""

import re
from typing import Callable

from services.doc_image_service import image_url

# Hard cap on pictures per answer. The tool description asks for at most 3; a
# model that ignores it must not be able to fill the screen with pages.
MAX_IMAGES_PER_ANSWER = 6

# Characters that would end the alt text, start a link or become a document
# link (#123) inside it.
_ALT_UNSAFE = re.compile(r"[\[\]()#*_`<>|]")


def with_images(text: str, shown: list, _: Callable[[str], str]) -> str:
    """``text`` with one image block per shown picture appended.

    ``shown`` holds ImageDisplayEvents in call order. A picture whose URL is
    already in the text is not added twice. The caption line carries ``#ID``,
    which the chat's link injection turns into the usual document link.
    """
    blocks = []
    for ev in (shown or [])[:MAX_IMAGES_PER_ANSWER]:
        url = image_url(ev.doc_id, ev.page, ev.index)
        if url in (text or ""):
            continue
        alt = " ".join(_ALT_UNSAFE.sub(" ", ev.caption or "").split()) or _("Document page")
        where = _("#{doc}, page {page}").format(doc=ev.doc_id, page=ev.page)
        blocks.append(f'![{alt}]({url})\n<span class="chat-img-cap">{alt} · {where}</span>')
    if not blocks:
        return text
    return ((text or "").rstrip() + "\n\n" + "\n\n".join(blocks)).strip()

"""An underscore inside a word is literal, everywhere a user reads markdown.

markdown2 predates CommonMark and eats intraword underscores as emphasis, which
turns `26_08_26_Heizkostenabrechnung` — a real document filename the model
quotes back — into italic fragments with the underscores gone. The user then
reads the mangled name as the document's actual name.
"""

import markdown2
import pytest

from services.markdown_text import escape_intraword_underscores as esc

_EXTRAS = ["fenced-code-blocks", "tables"]


def _html(text: str) -> str:
    return markdown2.markdown(esc(text), extras=_EXTRAS)


def test_filename_survives_verbatim():
    name = "26_08_26_Heizkostenabrechnung_Weisshoferstr59_2025-2026"
    assert name in _html(name)
    assert "<em>" not in _html(name)


def test_deliberate_emphasis_still_works():
    html = _html("_italic_ and __bold__ and **strong**")
    assert "<em>italic</em>" in html
    assert "<strong>bold</strong>" in html
    assert "<strong>strong</strong>" in html


def test_intraword_double_underscore_is_literal_too():
    assert "a__b__c" in _html("a__b__c")


@pytest.mark.parametrize(
    "text",
    [
        "code `a_b_c` here",
        "```\nx_y_z\n```",
        "[label](http://example.com/a_b/c_d)",
    ],
)
def test_protected_regions_are_untouched(text):
    """A backslash inside a code span or a link target renders verbatim, so
    those regions must not be escaped at all."""
    assert esc(text) == text


def test_html_attributes_are_not_escaped_but_their_text_is():
    """Chat markdown carries injected <a> elements; escaping inside the tag
    would put a backslash into an attribute value."""
    out = esc('<a href="#" data-doc-id="7">x_y</a>')
    assert '<a href="#" data-doc-id="7">' in out
    assert "x\\_y" in out
    assert "x_y</a>" in markdown2.markdown(out, extras=_EXTRAS)


def test_text_without_underscores_is_returned_unchanged():
    s = "nothing to do here"
    assert esc(s) is s

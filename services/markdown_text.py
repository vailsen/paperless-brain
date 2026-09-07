"""Markdown text fixups applied before handing a string to markdown2.

markdown2 predates CommonMark and still treats an underscore *inside* a word as
emphasis, so `26_08_26_Heizkostenabrechnung_Weisshoferstr59` renders as italic
fragments with the underscores eaten. Document filenames, model ids and paths
are full of them, and the user reads the mangled name as the real one.

CommonMark (and therefore Obsidian) already says an intraword underscore is
literal, so escaping it is not a house rule — it is the behaviour the rest of
the ecosystem has. markdown2's `code-friendly` extra would fix it by disabling
underscore emphasis entirely, which also kills the legitimate `_italic_` a user
types in a vault note; escaping only the intraword case keeps both.
"""

import re

# Regions where an underscore is already literal and a backslash would show up
# verbatim: fenced blocks, inline code spans, HTML tags (chat markdown carries
# injected <a> elements), and link/image destinations.
_PROTECTED_RE = re.compile(
    r"(```.*?```|~~~.*?~~~|`[^`\n]*`|<[^<>\n]+>|\]\([^)\s]*\))",
    re.DOTALL,
)
# A *run* of underscores, and alphanumerics on both sides: "a__b" is as
# intraword as "a_b", while a leading "__bold__" keeps its emphasis because
# the run is not preceded by an alphanumeric.
_INTRAWORD_UNDERSCORE_RE = re.compile(r"(?<=[^\W_])(_+)(?=[^\W_])")


def escape_intraword_underscores(text: str) -> str:
    """Backslash-escape underscores that sit between two word characters."""
    if "_" not in text:
        return text
    parts = _PROTECTED_RE.split(text)
    # split() with one capturing group puts the protected regions at odd indices.
    for i in range(0, len(parts), 2):
        parts[i] = _INTRAWORD_UNDERSCORE_RE.sub(
            lambda m: "\\_" * len(m.group(1)), parts[i]
        )
    return "".join(parts)

# services/extraction_guard.py
"""Detect degenerate vision-model output before it reaches the index.

Local vision models occasionally fall into a repetition loop and emit the same
phrase thousands of times for one page. The sampling settings in
`services/vision.py` make this more likely on purpose: `presence_penalty` is
pinned to 0 because the model card's 1.5 causes the model to *drop* legitimate
repeated content (table rows, repeated JSON keys). Catching the loop afterwards
is the right trade — a lost table is worse than a rare retry.

Without a guard the failure is silent. A real case in the author's archive: one
page of a notarial contract extracted 119,165 words at a 0.004 unique-word
ratio, and nothing flagged it. It was only found two years later, indirectly,
via a chunking test.

Thresholds are calibrated against that 425-document / 1,333-page archive:

    signal              corrupt page   largest legitimate   margin
    page word count       119,165            2,294           52x
    unique-word ratio       0.0037           0.0902          24x

Both are set well inside those gaps. Note the ratio must stay low: dense tabular
documents (mileage logs, cost breakdowns) legitimately reach 0.09 because
"Datum:", "Posten:" and "Betrag:" repeat on every row.

A repeated-n-gram detector was evaluated and rejected — legitimate forms scored
*higher* (0.72) than the corrupt page (0.62), because boilerplate repeats.
"""

from dataclasses import dataclass

# 2.6x the largest legitimate page observed (2,294 words). A dense A4 page tops
# out around 1,200 words, so anything beyond this is not a real page.
MAX_PLAUSIBLE_PAGE_WORDS = 6000

# 3x below the lowest legitimate ratio observed (0.0902).
MIN_UNIQUE_WORD_RATIO = 0.03

# Below this, ratios are noise — a 50-word page of repeated form labels is fine.
MIN_WORDS_FOR_RATIO_CHECK = 200


@dataclass(frozen=True)
class GuardVerdict:
    """Result of inspecting one page's extracted text."""

    ok: bool
    reason: str = ""
    word_count: int = 0
    unique_ratio: float = 1.0

    def __bool__(self) -> bool:
        return self.ok


def inspect_page_text(text: str) -> GuardVerdict:
    """Judge whether extracted page text looks like a repetition loop.

    Two independent triggers — either is sufficient, and neither depends on the
    other being reliable:

    1. Implausible length. No real page holds this many words.
    2. Low unique-word ratio on a long-enough page: the signature of a loop.
    """
    words = (text or "").split()
    count = len(words)
    ratio = len(set(words)) / count if count else 1.0

    if count > MAX_PLAUSIBLE_PAGE_WORDS:
        return GuardVerdict(
            ok=False,
            reason=(
                f"implausible page length: {count} words "
                f"(limit {MAX_PLAUSIBLE_PAGE_WORDS})"
            ),
            word_count=count,
            unique_ratio=ratio,
        )

    if count >= MIN_WORDS_FOR_RATIO_CHECK and ratio < MIN_UNIQUE_WORD_RATIO:
        return GuardVerdict(
            ok=False,
            reason=(
                f"repetition loop: unique-word ratio {ratio:.4f} "
                f"over {count} words (limit {MIN_UNIQUE_WORD_RATIO})"
            ),
            word_count=count,
            unique_ratio=ratio,
        )

    return GuardVerdict(ok=True, word_count=count, unique_ratio=ratio)


def salvage_page_text(text: str) -> str:
    """Cut degenerate text down to the part most likely to be genuine.

    A loop starts partway through: the opening of the page is usually real
    content and only the tail repeats. Keeping the head preserves what can be
    preserved and caps the damage, rather than discarding the page entirely.
    """
    words = (text or "").split()
    if len(words) <= MAX_PLAUSIBLE_PAGE_WORDS:
        return text
    return " ".join(words[:MAX_PLAUSIBLE_PAGE_WORDS])

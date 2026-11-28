"""Model-written conversation titles (`services/chat_titles.py`)."""

from services import chat_edit, chat_titles as ct


def test_due_after_the_first_and_third_answer():
    assert [n for n in range(6) if ct.due(n)] == [1, 3]


def test_answer_count_ignores_empty_answers():
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
            {"role": "user", "content": "q2"}, {"role": "assistant", "content": "  "}]
    assert ct.answer_count(msgs) == 1


def test_clean_takes_the_first_line_and_strips_decoration():
    assert ct.clean('Title: "Kfz-Versicherung kündigen".') == "Kfz-Versicherung kündigen"
    assert ct.clean("<think>hmm</think>\n## **Tax return 2025**\nmore") == "Tax return 2025"
    assert ct.clean("") is None
    assert ct.clean("   \n ") is None
    assert ct.clean(" ".join(["word"] * 20)) is None  # a sentence, not a title
    long = ct.clean("Abrechnung " + "Nebenkosten " * 6)
    assert long and len(long) <= ct.MAX_TITLE_CHARS and not long.endswith(" ")


def test_request_keeps_the_first_question_and_the_newest_exchange():
    msgs = [chat_edit.make_note("carried over")]
    msgs += [{"role": "user", "content": "first question"}, {"role": "assistant", "content": "x" * 5000}]
    msgs += [{"role": "user", "content": "newest question"}, {"role": "assistant", "content": "Answer [^241-p3]."}]
    req = ct.build_request(msgs)
    assert "carried over" in req and "newest question" in req
    assert "[^241" not in req
    assert len(req) < ct.EXCERPT_CHARS + 200
    assert ct.build_request([]) == ""

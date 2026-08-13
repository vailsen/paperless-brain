"""The heuristic behind the forced-tool retry.

A model that answers "what did I buy?" without calling a tool did not remember
anything — it invented it. The guard looks only at the question, never at the
answer: it must fire on personal questions and stay quiet on general ones, or it
burns a request per turn for nothing.
"""

import pytest

from services.chat_service import _last_user_text, _needs_tool_use


@pytest.mark.parametrize(
    "question",
    [
        "Wie heißt der Drehmomentschlüssel, den ich gekauft habe?",
        "Wann ist mein Termin beim Orthopäden?",
        "Wie hoch war meine letzte Stromrechnung?",
        "Ich habe letztes Jahr eine Versicherung abgeschlossen — welche?",
        "What did I buy from Amazon in June?",
        "When is my dentist appointment?",
        "Show me my invoice from last month",
    ],
)
def test_personal_questions_need_a_tool(question):
    assert _needs_tool_use(question)


@pytest.mark.parametrize(
    "question",
    [
        "Was ist ein Drehmomentschlüssel?",
        "Wie funktioniert ein Drehmomentschlüssel?",
        "What is a torque wrench?",
        "Explain how OAuth works",
        "Erkläre mir den Unterschied zwischen SSD und HDD",
        "Danke!",
        "",
    ],
)
def test_general_questions_do_not(question):
    assert not _needs_tool_use(question)


def test_a_possessive_inside_a_general_question_does_not_fire():
    """"What is my tax rate" reads personal but "what is" wins — an explainer."""
    assert not _needs_tool_use("Was ist mein Grenzsteuersatz bei 50000 Euro?")


# ── Extracting the question from the message list ────────────────────────────


def test_last_user_text_takes_the_most_recent_user_turn():
    messages = [
        {"role": "user", "content": "erste Frage"},
        {"role": "assistant", "content": "Antwort"},
        {"role": "user", "content": "meine Rechnung?"},
    ]
    assert _last_user_text(messages) == "meine Rechnung?"


def test_last_user_text_handles_content_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {}},
                {"type": "text", "text": "was habe ich gekauft?"},
            ],
        }
    ]
    assert _last_user_text(messages) == "was habe ich gekauft?"


def test_last_user_text_without_a_user_message():
    assert _last_user_text([{"role": "assistant", "content": "hi"}]) == ""


def test_guard_ignores_a_tool_result_turn():
    """Tool results are user-role messages on the wire — not the question."""
    messages = [
        {"role": "user", "content": "meine Rechnung?"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "no results"},
    ]
    assert _needs_tool_use(_last_user_text(messages))

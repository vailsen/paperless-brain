"""Web sources in chat answers: [^Site name] → a link to a page a tool returned."""

from services.web_citations import link_web_refs, plain_web_refs, sources_from_trace

TRACE = [
    {"name": "web_search", "input": {"query": "ischial bursitis"}, "output": (
        "Web search: 'ischial bursitis'\n\n"
        "1. [Ischial Bursitis: Causes & Treatment](https://my.clevelandclinic.org/health/ischial-bursitis) (2024)\n"
        "   Ischial bursitis is …\n\n"
        "2. [Piriformis beim Radfahren](https://www.zommunique.com/piriformis)\n")},
    {"name": "web_fetch_page", "input": {"url": "https://my.clevelandclinic.org/health/ischial-bursitis"},
     "output": "[trafilatura] Content of https://my.clevelandclinic.org/health/ischial-bursitis:\n\nIschial …"},
    {"name": "web_fetch_page", "input": {"url": "https://blocked.example/x"},
     "output": "Web fetch failed: timeout"},
    {"name": "search", "input": {}, "output": "[Not a web result](https://evil.example/x)"},
]


def test_site_names_become_links_to_the_pages_the_tools_returned():
    text = "Schon brennt's. [^Cleveland Clinic][^Zommunique]\nWeiter."
    out = link_web_refs(text, TRACE)
    assert out == ("Schon brennt's. ([Cleveland Clinic](https://my.clevelandclinic.org/health/ischial-bursitis), "
                   "[Zommunique](https://www.zommunique.com/piriformis))\nWeiter.")


def test_a_name_no_tool_returned_is_plain_text_never_a_link():
    out = link_web_refs("Laut Studie.[^Mayo Clinic] Ende", TRACE)
    assert out == "Laut Studie. (Mayo Clinic) Ende"


def test_a_url_is_linked_only_if_a_tool_returned_it():
    assert "](https://made.up/x)" not in link_web_refs("A [^https://made.up/x]", TRACE)
    out = link_web_refs("A [^https://www.zommunique.com/piriformis]", TRACE)
    assert "(www.zommunique.com" not in out and "](https://www.zommunique.com/piriformis)" in out


def test_title_words_match_too():
    out = link_web_refs("Text [^Piriformis beim Radfahren]", TRACE)
    assert "](https://www.zommunique.com/piriformis)" in out


def test_document_citations_are_left_alone():
    text = "Rente [^32-p3] und Akte [^241]. Gemischt [^241][^Cleveland Clinic]"
    out = link_web_refs(text, TRACE)
    assert "[^32-p3]" in out and "[^241]." in out
    assert "[^241] ([Cleveland Clinic](https://my.clevelandclinic.org/health/ischial-bursitis))" in out


def test_only_web_tools_are_sources():
    assert all("evil.example" not in s["url"] for s in sources_from_trace(TRACE))


def test_render_fallback_for_old_answers():
    assert plain_web_refs("Fakt.[^Cleveland Clinic], [^Zommunique] Ende [^12]") == \
        "Fakt. (Cleveland Clinic, Zommunique) Ende [^12]"


def test_a_failed_fetch_is_no_source():
    assert "blocked.example" not in link_web_refs("A [^blocked]", TRACE)


def test_numbered_web_footnotes_are_links_not_document_one():
    from services.web_citations import link_web_refs, plain_web_refs

    text = ("Laufen kann es verschlimmern.[^1] Langfristiger Prozess.[^2]\n\n"
            "[^1]: [Runners World – Piriformis Syndrome](https://www.runnersworld.com/piri)\n"
            "[^2]: [Temporal Patterns](https://pubmed.ncbi.nlm.nih.gov/123/)\n")
    out = plain_web_refs(text)
    assert "[^1]" not in out and "[^2]" not in out and "]:" not in out
    assert "verschlimmern. ([Runners World – Piriformis Syndrome](https://www.runnersworld.com/piri))" in out
    # store time: only a page a tool returned keeps its link
    trace = [{"name": "web_search", "output": "[Runners](https://www.runnersworld.com/piri)"}]
    stored = link_web_refs(text, trace)
    assert "(https://www.runnersworld.com/piri)" in stored
    assert "(Temporal Patterns)" in stored and "pubmed" not in stored


def test_document_markers_without_definitions_stay():
    from services.web_citations import plain_web_refs

    assert plain_web_refs("Beitrag 112,77 €.[^241-p3]") == "Beitrag 112,77 €.[^241-p3]"


def test_web_sources_render_as_numbers_with_links_in_the_box(monkeypatch):
    import app_ui.pages.chat as chat

    monkeypatch.setattr(chat, "get_translator", lambda: (lambda s: s))
    out = chat._inject_doc_links("Aigner soll es werden. ([srf.ch](https://www.srf.ch/news/aigner)) "
                                 "Palmer unter Druck. ([Spiegel](https://www.spiegel.de/b))")
    assert "([srf.ch]" not in out and "(srf.ch)" not in out
    assert 'class="pb-cite pb-web-cite" href="https://www.srf.ch/news/aigner" target="_blank"' in out
    assert ">1</a>" in out and ">2</a>" in out
    assert 'pb-cite-list' in out and ">srf.ch/news/aigner</span>" in out and ">Spiegel</span>" in out
    long = chat._inject_doc_links("BVB gewinnt. ([msn.com](https://www.msn.com/de-de/nachrichten/other/"
                                  "live-bvb-mit-drei-startelf-wechseln-gegen-elversberg/ar-AA2gW518))")
    assert ">msn.com/de-de/nachrichten/" in long and "…" in long and "ar-AA2gW518</span>" in long

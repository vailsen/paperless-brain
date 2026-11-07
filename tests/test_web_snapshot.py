"""Web sources as pictures (`services/web_snapshot.py`): what is fetched, what is looked for."""

import asyncio


def test_only_public_addresses_are_fetched():
    from services.web_snapshot import _public

    assert not asyncio.run(_public("http://127.0.0.1:8080/settings"))
    assert not asyncio.run(_public("http://192.168.234.117/"))
    assert not asyncio.run(_public("file:///etc/passwd"))
    assert not asyncio.run(_public("javascript:alert(1)"))


def test_needles_go_from_the_whole_passage_to_shorter_pieces():
    from services.web_snapshot import _needles

    n = _needles("Continuing to run high mileage can worsen the condition. Rest helps a lot here.")
    assert n[0].startswith("Continuing") and n[0].endswith("here.")
    assert "Continuing to run high mileage can worsen the condition." in n
    assert len(n) == len(set(n))


def test_keys_are_checked_before_a_file_is_served():
    from services.web_snapshot import _key, valid_key

    assert valid_key(_key("https://a.org", "claim"))
    assert not valid_key("../../etc/passwd") and not valid_key("ABC")

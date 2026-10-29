"""A chat with a local model whose host is off wakes it (WoL) and waits for
it; a host woken that way sleeps again after 10 idle minutes
(`services/wol.ensure_awake`, `ollama_watchdog.shutdown_due`)."""

import asyncio
from datetime import timedelta

import pytest

from config.settings import settings

MODEL_URL = "http://10.0.0.5:11434/v1"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host: `up` decides whether it answers, `packets` what was sent."""
    from services import ollama_watchdog as wd
    from services import wol

    monkeypatch.setattr(wd, "_state_path", lambda: tmp_path / "power_state.json")
    monkeypatch.setattr(wd, "_last_touch_write", 0.0)
    monkeypatch.setattr(wd, "TOUCH_WRITE_INTERVAL_S", 0.0)
    monkeypatch.setattr(wd, "_role", "app")
    monkeypatch.setattr(settings, "ollama_host_lan_mac_address_wol", "aa:bb:cc:dd:ee:ff")
    monkeypatch.setattr(settings, "ollama_server", "")
    monkeypatch.setattr(settings, "ollama_wake_timeout_s", 60)
    state = {"up": [False], "packets": [], "asked": []}

    async def reachable(h, port=11434):
        state["asked"].append((h, port))
        return state["up"].pop(0) if len(state["up"]) > 1 else state["up"][0]

    async def no_sleep(s):
        return None

    monkeypatch.setattr(wd, "_host_reachable", reachable)
    monkeypatch.setattr(wol, "send_magic_packet", lambda mac, bc: state["packets"].append((mac, bc)))
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    return state


def test_a_host_that_answers_is_left_alone(host):
    from services.wol import ensure_awake

    host["up"] = [True]
    assert asyncio.run(ensure_awake(MODEL_URL)) == "up"
    assert host["packets"] == [] and host["asked"] == [("10.0.0.5", 11434)]


def test_an_off_host_is_woken_and_the_chat_waits_for_it(host):
    from services import ollama_watchdog as wd
    from services.wol import ensure_awake

    host["up"] = [False, False, False, True]
    waits = []
    assert asyncio.run(ensure_awake(MODEL_URL, on_wait=waits.append)) == "woken"
    assert host["packets"] == [("aa:bb:cc:dd:ee:ff", "10.0.0.255")]
    assert len(waits) == 3
    s = wd.state()
    assert s["wol_active"] and s["wol_by"] == "chat" and s["wol_host"] == "10.0.0.5"


def test_only_the_host_wol_controls_is_woken(host, monkeypatch):
    from services.wol import ensure_awake

    monkeypatch.setattr(settings, "ollama_server", "http://10.0.0.9:11434")
    assert asyncio.run(ensure_awake(MODEL_URL)) == "not_ours"
    monkeypatch.setattr(settings, "ollama_server", "")
    monkeypatch.setattr(settings, "ollama_host_lan_mac_address_wol", "")
    assert asyncio.run(ensure_awake(MODEL_URL)) == "not_ours"
    assert host["packets"] == []


def test_a_host_that_never_comes_up_times_out_and_stop_ends_the_wait(host, monkeypatch):
    import itertools

    from services.wol import ensure_awake

    clock = itertools.count(0, 10)  # every reading 10 s later; the loop reads it too
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    assert asyncio.run(ensure_awake(MODEL_URL)) == "timeout"
    monkeypatch.setattr("time.monotonic", lambda: 0)
    assert asyncio.run(ensure_awake(MODEL_URL, should_stop=lambda: True)) == "stopped"


def test_a_chat_woken_host_sleeps_after_ten_idle_minutes(host, monkeypatch):
    from services import ollama_watchdog as wd

    monkeypatch.setattr(settings, "ollama_idle_shutdown_minutes", 30)
    monkeypatch.setattr(settings, "ollama_chat_idle_shutdown_minutes", 10)
    wd.on_wol("10.0.0.5", by="chat")
    wd.touch()  # the chat uses it: still a chat's wake, not the dashboard's 30 min
    s = wd.state()
    assert s["wol_by"] == "chat"
    t = s["last_activity"]
    assert not wd.shutdown_due(t + timedelta(minutes=9))
    assert wd.shutdown_due(t + timedelta(minutes=11))
    wd.on_wol("10.0.0.5")  # the dashboard button: the normal idle time
    t = wd.state()["last_activity"]
    assert not wd.shutdown_due(t + timedelta(minutes=11))
    assert wd.shutdown_due(t + timedelta(minutes=31))

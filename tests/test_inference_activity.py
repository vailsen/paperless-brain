"""The dashboard's compute line: which requests run on the model host, what is
in memory, how fast the last generation was (`services/inference_activity.py`,
`app_ui/inference_status.py`)."""

import asyncio
import time

import pytest

from config.settings import settings

HOST = "http://10.0.0.5:11434/v1"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    return tmp_path


def test_a_request_is_listed_while_it_runs_and_leaves_its_speed(data):
    from services import inference_activity as ia

    host = ia.host_of(HOST)
    assert host == "10.0.0.5:11434"

    async def go():
        async with ia.track(HOST, "qwen3", "chat") as r:
            (running,) = ia.snapshot(host)["running"]
            assert running["model"] == "qwen3" and running["purpose"] == "chat"
            for _ in range(40):
                r.token()
            await asyncio.sleep(0.6)

    asyncio.run(go())
    snap = ia.snapshot(host)
    assert snap["running"] == [] and snap["last"]["kind"] == "stream" and 40 <= snap["last"]["tokens"]
    assert snap["last"]["tps"] > 0
    assert ia.snapshot("other:80") == {"running": [], "last": None}  # per host


def test_ollamas_own_timings_are_exact_and_a_whole_answer_is_an_estimate(data):
    from services import inference_activity as ia

    async def go(body):
        async with ia.track(HOST, "qwen3") as r:
            r.usage(body)

    asyncio.run(go({"eval_count": 200, "eval_duration": 4_000_000_000}))
    last = ia.snapshot(ia.host_of(HOST))["last"]
    assert last == {**last, "tps": 50.0, "kind": "exact", "purpose": "background"}
    asyncio.run(go({"usage": {"completion_tokens": 30}}))
    assert ia.snapshot(ia.host_of(HOST))["last"]["kind"] == "estimate"


def test_a_failing_request_is_not_left_running(data):
    from services import inference_activity as ia

    async def go():
        async with ia.track(HOST, "qwen3"):
            raise RuntimeError("500 from the server")

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert ia.snapshot(ia.host_of(HOST))["running"] == []


def test_a_dead_process_leaves_no_ghost_request(data):
    import json

    from services import inference_activity as ia

    p = data / "data" / "inference_activity.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"inflight": {"a": {"host": "10.0.0.5:11434", "model": "q", "purpose": "chat",
                                                "since": time.time(), "pid": 999999999}}, "last": {}}))
    assert ia.snapshot("10.0.0.5:11434")["running"] == []


def test_the_compute_lines():
    from app_ui.inference_status import lines

    def _(s):
        return s

    now = 1_800_000_000.0
    models = [{"name": "qwen3.8:30b", "size_vram": 18_200_000_000, "expires_at": "2027-01-15T08:03:20.123456789Z"}]
    snap = {"running": [{"purpose": "werkbank", "model": "qwen3.8:30b", "since": now - 42}],
            "last": {"tps": 41.4, "kind": "stream", "purpose": "chat", "at": now - 130}}
    out = lines(_, models, snap, now)
    assert out[0] == ("bolt", "Generating — Deep research · qwen3.8:30b · 0:42")
    assert out[1] == ("memory", "In memory: qwen3.8:30b · 18.2 GB")  # no unload time while generating
    assert out[2] == ("speed", "Last: 41 tokens/s · Chat · 2 min ago")
    idle = lines(_, models, {"running": [], "last": {**snap["last"], "kind": "estimate"}}, now)
    assert idle[0] == ("hourglass_empty", "Idle")
    assert idle[1][1].endswith("unloads in 3 min") and idle[2][1].startswith("Last: ≈ 41")
    assert lines(_, [], {"running": [], "last": None}, now) == [
        ("hourglass_empty", "Idle"), ("memory", "No model in memory")]

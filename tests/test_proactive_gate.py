"""Proactive Phase 2: inference gate, GPU yield, windows, budget, models, cloud."""

import asyncio
import logging
import sys
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def activity_file(tmp_path, monkeypatch):
    from services import gpu_yield

    p = tmp_path / "activity_state.json"
    monkeypatch.setattr(gpu_yield, "_state_path", lambda: p)
    monkeypatch.setattr(gpu_yield, "_last_touch_write", 0.0)
    monkeypatch.setattr(gpu_yield, "TOUCH_WRITE_INTERVAL_S", 0.0)
    return p


def _window_env(monkeypatch, max_docs=10):
    from config import proactive as cfg
    from proactive import clock

    now = clock.local_now()
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    monkeypatch.setenv("PROACTIVE_SWEEP_WINDOWS", f"{start}-{end}/{max_docs}")
    cfg.proactive_settings.cache_clear()


class FakeGate:
    def __init__(self, ready):
        self.ready = ready
        self.asked = 0
        self.woke_host = False

    async def local_ready(self, may_wake=True):
        self.asked += 1
        return self.ready


def _registry(monkeypatch, per_user):
    from proactive import models

    monkeypatch.setattr(models, "_registry", lambda user, token: per_user.get(user, []))


def _m(name, model, lane="local", enabled=True):
    return {"name": name, "model": model, "lane": lane, "enabled": enabled}


def _optin(user, **kw):
    from proactive import prefs

    p = prefs.get(user)
    p.optin = True
    for k, v in kw.items():
        setattr(p, k, v)
    prefs.save(p)


# ── Gate ─────────────────────────────────────────────────────────────────────


async def _no_proxy(url):
    return None


def _proxy(host):
    async def status(url):
        return {"host": host[0]}
    return status


async def _no_models(url):
    return set()


def test_plain_server_that_does_not_answer_stays_closed_and_backs_off():
    from proactive.gate import InferenceGate

    probes = []

    async def probe(url):
        probes.append(url)
        return False

    t = [0.0]
    g = InferenceGate(server="http://10.0.0.5:11434", probe_fn=probe, status_fn=_no_proxy,
                      monotonic=lambda: t[0])
    assert asyncio.run(g.local_ready()) is False
    assert asyncio.run(g.local_ready()) is False  # within backoff: not even probed
    assert len(probes) == 1
    t[0] += 301
    asyncio.run(g.local_ready())
    assert len(probes) == 2


def test_behind_a_proxy_a_sleeping_host_is_ready_only_when_we_may_wake_it():
    """Inside a window `asleep` is ready — the job's request wakes the host
    through the proxy. Outside one only `up` is, so nothing wakes it by day."""
    from proactive.gate import ASLEEP_RECHECK_S, InferenceGate

    host, t = ["asleep"], [0.0]

    async def probe(url):
        raise AssertionError("behind a proxy the plain probe is never needed")

    g = InferenceGate(server="http://10.0.0.112:11434", probe_fn=probe, status_fn=_proxy(host),
                      list_models_fn=_no_models, monotonic=lambda: t[0])
    assert asyncio.run(g.local_ready(may_wake=False)) is False
    assert asyncio.run(g.local_ready(may_wake=True)) is True     # a window opened
    assert g.woke_host
    # Ready because it was asleep: that does not carry over to "outside a window".
    assert asyncio.run(g.local_ready(may_wake=False)) is False
    t[0] += ASLEEP_RECHECK_S + 1
    host[0] = "up"                                                # someone switched it on
    assert asyncio.run(g.local_ready(may_wake=False)) is True


def test_behind_a_proxy_a_host_shutting_down_waits():
    from proactive.gate import InferenceGate

    g = InferenceGate(server="http://p:11434", status_fn=_proxy(["shutting_down"]),
                      list_models_fn=_no_models)
    assert asyncio.run(g.local_ready(may_wake=True)) is False


def test_fixed_model_missing_on_host_refuses():
    from proactive.gate import InferenceGate, OperatorModelMissing

    async def probe(url):
        return True

    async def models(url):
        return {"qwen3:32b", "llama3:latest"}

    g = InferenceGate(server="http://h:11434", operator_models=["gemma3:27b"],
                      probe_fn=probe, status_fn=_no_proxy, list_models_fn=models)
    with pytest.raises(OperatorModelMissing, match="gemma3:27b"):
        asyncio.run(g.local_ready())
    ok = InferenceGate(server="http://h:11434", operator_models=["llama3"],
                       probe_fn=probe, status_fn=_no_proxy, list_models_fn=models)
    assert asyncio.run(ok.local_ready()) is True


def test_worker_refuses_to_start_when_fixed_model_is_missing(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from config.settings import settings
    from proactive import gate, worker

    monkeypatch.setenv("PROACTIVE_MODEL_FIXED", "nope:1b")
    cfgmod.proactive_settings.cache_clear()
    monkeypatch.setattr(settings, "ollama_server", "http://h:11434")

    async def probe(url, timeout=3.0):
        return True

    async def models(url, timeout=5.0):
        return {"qwen3:32b"}

    monkeypatch.setattr(gate, "probe", probe)
    monkeypatch.setattr(gate, "list_models", models)
    monkeypatch.delitem(sys.modules, "services.clients", raising=False)  # other tests load it
    assert asyncio.run(worker._amain()) == 2


def test_worker_refuses_operator_model_without_ollama_server(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from config.settings import settings
    from proactive import worker

    monkeypatch.setenv("PROACTIVE_MODEL_DEFAULT", "qwen3:32b")
    cfgmod.proactive_settings.cache_clear()
    monkeypatch.setattr(settings, "ollama_server", "")
    monkeypatch.delitem(sys.modules, "services.clients", raising=False)
    assert asyncio.run(worker._amain()) == 2


# ── Worker + gate: LLM jobs wait, others run ─────────────────────────────────


def test_asleep_llm_jobs_stay_queued_others_run(proactive_on):
    from proactive import jobs
    from proactive.worker import Worker

    ran = []

    @jobs.handler("t2_llm", needs_llm=True)
    async def llm_job(job):
        ran.append("llm")

    @jobs.handler("t2_reminder")
    async def reminder(job):
        ran.append("reminder")

    llm_id = jobs.enqueue("t2_llm")
    jobs.enqueue("t2_reminder")
    w = Worker(gate=FakeGate(ready=False))
    asyncio.run(w.tick_once())
    assert ran == ["reminder"]
    assert jobs.get(llm_id).status == "queued"


def test_gate_is_not_asked_when_no_llm_job_is_due(proactive_on):
    from proactive import jobs
    from proactive.worker import Worker

    @jobs.handler("t2_plain")
    async def plain(job):
        pass

    jobs.enqueue("t2_plain")
    g = FakeGate(ready=True)
    asyncio.run(Worker(gate=g).tick_once())
    assert g.asked == 0  # nothing local to run → never wakes the host


# ── Model resolution ─────────────────────────────────────────────────────────


def test_resolution_order(proactive_on, monkeypatch, caplog):
    from config import proactive as cfgmod
    from proactive.models import resolve_model

    _registry(monkeypatch, {"alice": [_m("Qwen local", "qwen3:32b"), _m("Claude", "claude-x", lane="api")]})
    _optin("alice", model="Qwen local", chat_model="Claude")
    r = resolve_model("alice", "tok")
    assert (r.name, r.lane, r.source) == ("Qwen local", "local", "user")

    # stale choice → default, with a log line
    _optin("alice", model="Gone model")
    monkeypatch.setenv("PROACTIVE_MODEL_DEFAULT", "gemma3:27b")
    cfgmod.proactive_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger="proactive.models"):
        r = resolve_model("alice", "tok")
    assert (r.name, r.source) == ("gemma3:27b", "default")
    assert any("Gone model" in rec.message for rec in caplog.records)

    # no default → chat model, but a cloud chat model is not allowed by default
    monkeypatch.delenv("PROACTIVE_MODEL_DEFAULT")
    cfgmod.proactive_settings.cache_clear()
    assert resolve_model("alice", "tok") is None
    monkeypatch.setenv("PROACTIVE_CLOUD_ALLOWED", "true")
    cfgmod.proactive_settings.cache_clear()
    r = resolve_model("alice", "tok")
    assert (r.name, r.lane, r.source) == ("Claude", "cloud", "chat")


def test_allowlist_restricts_the_choice(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from proactive.models import allowed_registry_models, resolve_model

    _registry(monkeypatch, {"bob": [_m("Qwen", "qwen3:32b"), _m("Llama", "llama3:8b"), _m("Off", "x", enabled=False)]})
    monkeypatch.setenv("PROACTIVE_MODEL_ALLOWLIST", "qwen3:32b")
    cfgmod.proactive_settings.cache_clear()
    assert [m["name"] for m in allowed_registry_models("bob", "tok")] == ["Qwen"]
    _optin("bob", model="Llama")
    assert resolve_model("bob", "tok") is None  # not allowed, no default, no chat model


def test_fixed_model_overrides_and_choices_come_back(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from proactive import prefs
    from proactive.models import resolve_model

    _registry(monkeypatch, {"alice": [_m("Qwen", "qwen3:32b")], "bob": [_m("Llama", "llama3:8b")]})
    _optin("alice", model="Qwen")
    _optin("bob", model="Llama")
    monkeypatch.setenv("PROACTIVE_MODEL_FIXED", "gemma3:27b")
    cfgmod.proactive_settings.cache_clear()
    keys = {resolve_model(u, "tok").key for u in ("alice", "bob")}
    assert keys == {"local:gemma3:27b"}
    assert prefs.get("alice").model == "Qwen"  # kept, not deleted
    monkeypatch.delenv("PROACTIVE_MODEL_FIXED")
    cfgmod.proactive_settings.cache_clear()
    assert resolve_model("alice", "tok").name == "Qwen"
    assert resolve_model("bob", "tok").name == "Llama"


# ── Grouping: one swap per model per window ──────────────────────────────────


def test_two_users_two_models_one_swap(proactive_on, monkeypatch):
    from proactive import jobs, windows
    from proactive.models import ResolvedModel
    from proactive.worker import Worker

    _window_env(monkeypatch)
    order = []

    @jobs.handler("t2_work", needs_llm=True)
    async def work(job):
        order.append(job.model_key)

    a = ResolvedModel("Qwen", "local", "user", "qwen3:32b")
    b = ResolvedModel("Llama", "local", "user", "llama3:8b")
    for i in range(4):  # interleaved: a b a b a b a b
        jobs.enqueue("t2_work", "alice", {"i": i}, model=a)
        jobs.enqueue("t2_work", "bob", {"i": i}, model=b)
    w = Worker(gate=FakeGate(ready=True))
    asyncio.run(w.tick_once())
    assert order == [a.key] * 4 + [b.key] * 4
    assert w.model_swaps == 1
    win = windows.current()
    assert [r["model"] for r in windows.log_rows(win.start_iso)] == [a.key, b.key]


def test_fixed_model_means_zero_swaps(proactive_on, monkeypatch):
    from proactive import jobs
    from proactive.models import ResolvedModel
    from proactive.worker import Worker

    @jobs.handler("t2_fixed", needs_llm=True)
    async def work(job):
        pass

    fixed = ResolvedModel("gemma3:27b", "local", "fixed", "gemma3:27b")
    for u in ("alice", "bob", "carol"):
        jobs.enqueue("t2_fixed", u, model=fixed)
    w = Worker(gate=FakeGate(ready=True))
    asyncio.run(w.tick_once())
    assert w.model_swaps == 0


# ── Sweep: budget split round-robin, stops at window end ─────────────────────


def _sweep_setup(monkeypatch, backlog):
    from proactive import users
    from proactive.tasks import sweep

    monkeypatch.setattr(users, "eligible", lambda: [(u, f"tok-{u}") for u in sorted(backlog)])
    monkeypatch.setattr(users, "token", lambda u: f"tok-{u}")

    async def ids_after(tok, after, limit=1):
        u = tok.removeprefix("tok-")
        return [d for d in backlog[u] if d > after][:limit]

    monkeypatch.setattr(sweep, "ids_after", ids_after)
    done = []

    async def processor(user, tok, doc_id, model):
        done.append((user, doc_id))
        return True

    sweep.set_processor(processor)
    _registry(monkeypatch, {u: [_m("Qwen", "qwen3:32b")] for u in backlog})
    for u in backlog:
        _optin(u, model="Qwen")
    return done


def test_budget_is_split_evenly_and_respected(proactive_on, monkeypatch):
    from proactive import jobs, windows
    from proactive.tasks import sweep
    from proactive.worker import Worker

    _window_env(monkeypatch, max_docs=10)
    backlog = {u: list(range(base, base + 50)) for u, base in (("alice", 100), ("bob", 300), ("carol", 500))}
    done = _sweep_setup(monkeypatch, backlog)
    try:
        w = Worker(gate=FakeGate(ready=True))
        for _ in range(20):
            jobs.enqueue(sweep.SWEEP_ROUND, dedupe_key=f"schedule:{sweep.SWEEP_ROUND}")
            asyncio.run(w.tick_once())
        per_user = {u: sum(1 for x, _ in done if x == u) for u in backlog}
        assert sum(per_user.values()) == 10
        assert sorted(per_user.values()) == [3, 3, 4]
        # in id order per user, cursor advanced
        assert [d for u, d in done if u == "alice"] == sorted(d for u, d in done if u == "alice")
        win = windows.current()
        assert windows.processed(win.start_iso) == 10
        assert any(r["stop_reason"] == "budget" for r in windows.log_rows(win.start_iso))
    finally:
        sweep.set_processor(None)


def test_sweep_stops_at_window_end_with_budget_left(proactive_on, monkeypatch):
    from proactive import clock, jobs, windows
    from proactive.tasks import sweep
    from proactive.worker import Worker

    _window_env(monkeypatch, max_docs=200)
    backlog = {"alice": list(range(1, 100)), "bob": list(range(200, 300))}
    done = _sweep_setup(monkeypatch, backlog)
    try:
        w = Worker(gate=FakeGate(ready=False))  # docs get queued but cannot run yet
        jobs.enqueue(sweep.SWEEP_ROUND, dedupe_key=f"schedule:{sweep.SWEEP_ROUND}")
        asyncio.run(w.tick_once())
        win = windows.current()
        assert jobs.pending_count(kind=sweep.SWEEP_DOC) == 2
        clock.advance(timedelta(hours=2))  # the window closes
        w.gate.ready = True
        asyncio.run(w.tick_once())
        assert done == []  # dropped, not run
        assert jobs.pending_count(kind=sweep.SWEEP_DOC) == 0
        assert any(r["stop_reason"] == "window_end" for r in windows.log_rows(win.start_iso))
        assert sweep.cursor("alice") == (0, False)  # first doc again next window
    finally:
        sweep.set_processor(None)


def test_finished_cursor_until_version_bump(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from proactive import jobs
    from proactive.tasks import sweep
    from proactive.worker import Worker

    _window_env(monkeypatch, max_docs=100)
    done = _sweep_setup(monkeypatch, {"alice": [1, 2]})
    try:
        w = Worker(gate=FakeGate(ready=True))
        for _ in range(5):
            jobs.enqueue(sweep.SWEEP_ROUND, dedupe_key=f"schedule:{sweep.SWEEP_ROUND}")
            asyncio.run(w.tick_once())
        assert done == [("alice", 1), ("alice", 2)]
        assert sweep.cursor("alice") == (2, True)
        monkeypatch.setenv("PROACTIVE_ANALYSIS_VERSION", "2")
        cfgmod.proactive_settings.cache_clear()
        assert sweep.cursor("alice") == (0, False)  # re-sweep for the new version
    finally:
        sweep.set_processor(None)


# ── Cloud ────────────────────────────────────────────────────────────────────


def test_cloud_cap_defers_until_tomorrow(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from proactive import llm
    from proactive.jobs import Defer
    from proactive.models import ResolvedModel

    monkeypatch.setenv("PROACTIVE_CLOUD_ALLOWED", "true")
    monkeypatch.setenv("PROACTIVE_CLOUD_MAX_DOCS_PER_USER_DAY", "2")
    cfgmod.proactive_settings.cache_clear()
    cloud = ResolvedModel("Claude", "cloud", "user", "claude-x")
    for _ in range(2):
        llm.check_cloud_cap("alice", cloud)
        llm.record_cloud_doc("alice", cloud)
    with pytest.raises(Defer) as e:
        llm.check_cloud_cap("alice", cloud)
    assert e.value.seconds >= 60
    llm.check_cloud_cap("bob", cloud)  # per user


def test_cloud_not_allowed_no_llm_job_but_reminders_run(proactive_on, monkeypatch):
    """A user whose only model is cloud gets no LLM work; non-LLM jobs still run."""
    from proactive import jobs
    from proactive.models import resolve_model
    from proactive.worker import Worker

    _registry(monkeypatch, {"alice": [_m("Claude", "claude-x", lane="api")]})
    _optin("alice", model="Claude", chat_model="Claude")
    assert resolve_model("alice", "tok") is None
    ran = []

    @jobs.handler("t2_deadline_reminder")
    async def reminder(job):
        ran.append(job.user)

    jobs.enqueue("t2_deadline_reminder", "alice")
    asyncio.run(Worker(gate=FakeGate(ready=False)).tick_once())
    assert ran == ["alice"]


def test_ids_after_filters_itself_because_paperless_ignores_id_gt(monkeypatch):
    """Paperless has no `id__gt` filter and ignores unknown ones: the old query
    returned #2… as "new" every five minutes (seen live, 2026-10-27)."""
    from proactive import paperless_ro

    archive = list(range(2, 1203))  # 1,201 documents → three pages of 500

    class R:
        status_code = 200

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

        def raise_for_status(self):
            pass

    calls = []

    async def fake_get(token, path, params=None):
        calls.append(params)
        ordered = sorted(archive, reverse=params.get("ordering") == "-id")
        size, page = params["page_size"], params.get("page", 1)
        chunk = ordered[(page - 1) * size: page * size]
        nxt = "x" if page * size < len(ordered) else None
        return R({"results": [{"id": i} for i in chunk], "next": nxt})

    monkeypatch.setattr(paperless_ro, "_get", fake_get)
    assert asyncio.run(paperless_ro.ids_after("t", 1202, 100)) == []
    assert len(calls) == 1                                  # newest page reached the cursor
    assert asyncio.run(paperless_ro.ids_after("t", 1199, 100)) == [1200, 1201, 1202]
    assert asyncio.run(paperless_ro.ids_after("t", 115, 2)) == [116, 117]
    assert asyncio.run(paperless_ro.ids_after("t", 0, 3)) == [2, 3, 4]


def test_app_activity_makes_the_worker_wait(activity_file, monkeypatch):
    from services import gpu_yield

    monkeypatch.setattr(gpu_yield, "_role", "worker")
    gpu_yield.touch()                            # the worker's own requests do not count
    assert not gpu_yield.app_busy()
    monkeypatch.setattr(gpu_yield, "_role", "app")
    gpu_yield.touch()                            # a user chats
    t = datetime.now()
    assert gpu_yield.app_busy(t + timedelta(seconds=60))
    assert not gpu_yield.app_busy(t + timedelta(seconds=gpu_yield.APP_YIELD_S + 5))
    assert activity_file.exists()


def test_worker_leaves_llm_jobs_queued_while_the_app_is_busy(proactive_on, monkeypatch):
    from proactive import jobs
    from proactive.worker import Worker
    from services import gpu_yield

    class Open:
        woke_host = False

        async def local_ready(self, may_wake=True):
            return True

    jobs.enqueue("doc_new", "", {"doc_id": 1}, needs_llm=True)
    w = Worker(gate=Open())
    monkeypatch.setattr(gpu_yield, "app_busy", lambda now=None: True)
    assert asyncio.run(w._local_ok()) is False
    monkeypatch.setattr(gpu_yield, "app_busy", lambda now=None: False)
    assert asyncio.run(w._local_ok()) is True


def test_analyse_now_works_outside_any_window_and_stops_at_the_count(proactive_on, monkeypatch):
    from proactive.tasks import sweep
    from proactive.worker import Worker

    monkeypatch.setenv("PROACTIVE_SWEEP_WINDOWS", "")      # no window at all
    from config import proactive as cfg

    cfg.proactive_settings.cache_clear()
    done = _sweep_setup(monkeypatch, {"alice": [10, 11, 12, 13, 14]})

    class Open:
        woke_host = False

        async def local_ready(self, may_wake=True):
            return True

    assert sweep.start_now("alice", 3) == 3
    sweep.start_now("alice", 3)                            # a second click: still one chain
    w = Worker(gate=Open())
    for _ in range(6):
        asyncio.run(w.tick_once())
    assert done == [("alice", 10), ("alice", 11), ("alice", 12)]
    assert sweep.now_remaining("alice") == 0
    assert sweep.cursor("alice") == (12, False)            # the night continues from here

    sweep.start_now("alice", 10)                           # more than is left
    for _ in range(6):
        asyncio.run(w.tick_once())
    assert [d for _, d in done] == [10, 11, 12, 13, 14]
    assert sweep.cursor("alice")[1] is True and sweep.now_remaining("alice") == 0


@pytest.fixture
def vault_sync_env(proactive_on, monkeypatch):
    from proactive import internal_client, users
    from vault import notes

    monkeypatch.setattr(users, "eligible", lambda: [("alice", "t-a"), ("bob", "t-b"), ("carol", "t-c")])
    pending = {"alice": {"PaperlessBrain Findings/Notices/x.md"}, "bob": {"Inbox/todo.md"}, "carol": set()}

    async def pending_rel_paths(user):
        return pending[user]

    synced = []

    async def fake_sync(user):
        synced.append(user)
        return {"ok": True}

    monkeypatch.setattr(notes, "pending_rel_paths", pending_rel_paths)
    monkeypatch.setattr(internal_client, "vault_sync", fake_sync)
    return synced


def _check():
    from proactive.jobs import Job
    from proactive.tasks.vault_sync import vault_sync_check

    asyncio.run(vault_sync_check(Job(id=0, kind="vault_sync_check", user="", payload={},
                                     needs_llm=False, status="running", attempts=0, run_after="")))


def test_vault_sync_for_every_vault_with_pending_changes(vault_sync_env):
    _check()
    # the worker's findings and the user's own Obsidian edit alike; a clean vault is left alone
    assert vault_sync_env == ["alice", "bob"]


def test_vault_sync_runs_while_the_batch_runs(vault_sync_env):
    from proactive import clock, jobs
    from proactive.db import connect

    jobs.enqueue("sweep_doc", "alice", {"doc_id": 1}, needs_llm=True)
    with connect() as conn:                       # one just finished: batch running
        conn.execute("UPDATE jobs SET status = 'done', finished_at = ?", (clock.now_iso(),))
    _check()
    assert vault_sync_env == ["alice", "bob"]     # a sweep night no longer delays it until morning


def test_no_vault_sync_while_a_user_chats(vault_sync_env, monkeypatch):
    from services import gpu_yield

    monkeypatch.setattr(gpu_yield, "app_busy", lambda now=None: True)
    _check()
    assert vault_sync_env == []
    monkeypatch.setattr(gpu_yield, "app_busy", lambda now=None: False)
    _check()
    assert vault_sync_env == ["alice", "bob"]


# ── Waking only inside the sweep windows (the evening wake-ups) ──────────────


class RecordingGate:
    """A real InferenceGate in front of a wake-on-request proxy whose host is
    asleep. `woken` records each time the gate sent LLM work to the sleeping
    host — its first request wakes it through the proxy."""

    def __init__(self):
        from proactive.gate import InferenceGate

        self.woken = []

        async def status(url):
            return {"host": "up" if self.woken else "asleep"}

        g = InferenceGate(server="http://10.0.0.112:11434", status_fn=status, list_models_fn=_no_models)
        ready = g.local_ready

        async def local_ready(may_wake=True):
            asleep = not self.woken
            ok = await ready(may_wake=may_wake)
            if ok and asleep:
                self.woken.append(g.server)
            return ok

        g.local_ready = local_ready
        self.gate = g


def _no_window(monkeypatch):
    from config import proactive as cfgmod
    from proactive import clock

    far = clock.local_now() + timedelta(hours=6)
    monkeypatch.setenv("PROACTIVE_SWEEP_WINDOWS", f"{far:%H:%M}-{far + timedelta(hours=1):%H:%M}/10")
    cfgmod.proactive_settings.cache_clear()


def _llm_handlers(ran):
    from proactive import jobs

    for kind in ("doc_new_t", "deep_dive", "sweep_now", "dossier_revisit", "sweep_doc"):
        async def h(job, kind=kind):
            ran.append(kind)
        jobs.handler(kind, needs_llm=True)(h)


@pytest.fixture
def restore_handlers():
    from proactive import jobs

    saved = dict(jobs._HANDLERS)
    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(saved)


def test_outside_a_window_a_new_document_never_wakes_the_host(proactive_on, monkeypatch, restore_handlers):
    from proactive import jobs
    from proactive.worker import Worker

    _no_window(monkeypatch)
    ran = []
    _llm_handlers(ran)
    jobs.enqueue("doc_new_t")
    rg = RecordingGate()
    asyncio.run(Worker(gate=rg.gate).tick_once())
    assert rg.woken == [] and ran == []
    assert jobs.pending_count(kind="doc_new_t") == 1  # waits for a window or the host


def test_outside_a_window_backlog_jobs_are_not_even_considered(proactive_on, monkeypatch, restore_handlers):
    from proactive import jobs
    from proactive.worker import Worker

    _no_window(monkeypatch)
    ran = []
    _llm_handlers(ran)
    jobs.enqueue("dossier_revisit", "alice")
    asked = []

    class G:
        woke_host = False

        async def local_ready(self, may_wake=True):
            asked.append(may_wake)
            return True

    asyncio.run(Worker(gate=G()).tick_once())
    assert asked == [] and ran == []  # the gate was not even asked


def test_stale_backlog_is_dropped_when_the_window_closed(proactive_on, monkeypatch, restore_handlers):
    from proactive import jobs
    from proactive.worker import Worker

    _no_window(monkeypatch)
    ran = []
    _llm_handlers(ran)
    jid = jobs.enqueue("sweep_doc", "alice", {"doc_id": 7, "window_start": "2026-10-27T00:00:00Z"})
    asyncio.run(Worker(gate=RecordingGate().gate).tick_once())
    j = jobs.get(jid)
    assert j.status == "done" and "window" in j.last_error and ran == []


def test_a_fresh_user_request_may_wake_a_stale_one_may_not(proactive_on, monkeypatch, restore_handlers):
    from proactive import clock, jobs
    from proactive.worker import Worker

    _no_window(monkeypatch)
    ran = []
    _llm_handlers(ran)
    jobs.enqueue("deep_dive", "alice", {"finding_id": "x"})
    clock.advance(timedelta(hours=2))  # the request is old by now (kept deferring)
    rg = RecordingGate()
    asyncio.run(Worker(gate=rg.gate).tick_once())
    assert rg.woken == [] and ran == []

    jobs.enqueue("sweep_now", "alice")  # "Analyse now", just pressed
    asyncio.run(Worker(gate=rg.gate).tick_once())
    assert len(rg.woken) == 1 and "sweep_now" in ran


def test_inside_a_window_new_documents_wake_the_host(proactive_on, monkeypatch, restore_handlers):
    from proactive import jobs
    from proactive.worker import Worker

    _window_env(monkeypatch)
    ran = []
    _llm_handlers(ran)
    jobs.enqueue("doc_new_t")
    rg = RecordingGate()
    asyncio.run(Worker(gate=rg.gate).tick_once())
    assert len(rg.woken) == 1 and ran == ["doc_new_t"]


# ── Proxy headers: background profile, no wake outside a window ─────────────


def _header_capture(ran):
    from proactive import jobs
    from services import model_host

    async def h(job):
        ran.append(model_host.headers())
    jobs.handler("doc_new_t", needs_llm=True)(h)


class _Up:
    woke_host = False

    async def local_ready(self, may_wake=True):
        return True


def test_worker_requests_outside_a_window_must_not_wake_the_host(proactive_on, monkeypatch, restore_handlers):
    from proactive import jobs
    from proactive.worker import Worker
    from services import gpu_yield

    monkeypatch.setattr(gpu_yield, "_role", "worker")
    _no_window(monkeypatch)
    ran = []
    _header_capture(ran)
    jobs.enqueue("doc_new_t")
    asyncio.run(Worker(gate=_Up()).tick_once())       # the host happens to be on
    assert ran == [{"X-Idle-Profile": "background", "X-No-Wake": "1"}]


def test_worker_requests_inside_a_window_may_wake_the_host(proactive_on, monkeypatch, restore_handlers):
    from proactive import jobs
    from proactive.worker import Worker
    from services import gpu_yield, model_host

    monkeypatch.setattr(gpu_yield, "_role", "worker")
    _window_env(monkeypatch)
    ran = []
    _header_capture(ran)
    jobs.enqueue("doc_new_t")
    asyncio.run(Worker(gate=_Up()).tick_once())
    assert ran == [{"X-Idle-Profile": "background"}]
    assert model_host.headers() == {"X-Idle-Profile": "background"}  # no leak after the job


def test_the_apps_requests_carry_no_proxy_headers(monkeypatch):
    from services import gpu_yield, model_host

    monkeypatch.setattr(gpu_yield, "_role", "app")
    assert model_host.headers() == {}
    with model_host.no_wake():
        assert model_host.headers() == {"X-No-Wake": "1"}
    assert model_host.headers() == {}

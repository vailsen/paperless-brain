"""Proactive Phase 1: switch, config, DB, tick loop, job runner, shutdown."""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── Feature switch ───────────────────────────────────────────────────────────


def test_disabled_worker_exits_zero_and_creates_nothing(proactive_off):
    from proactive import worker

    assert worker.main() == 0
    assert not proactive_off.exists()


def test_disabled_db_refuses(proactive_off):
    from proactive.db import ProactiveDisabled, connect

    with pytest.raises(ProactiveDisabled):
        with connect():
            pass
    assert not proactive_off.exists()


def test_disabled_app_registers_nothing(proactive_off):
    from proactive.app import register

    assert register() is False
    assert not proactive_off.exists()


def test_disabled_settings_page_has_no_proactive_group():
    src = (REPO / "app_ui" / "pages" / "settings.py").read_text()
    # The group (and with it the only import of the proactive settings card)
    # exists only behind the switch.
    assert "if _proactive_enabled():" in src
    i_gate = src.index("if _proactive_enabled():")
    assert src.index('"proactive",') > i_gate


def test_worker_never_imports_services_clients(tmp_path):
    env = dict(os.environ, PROACTIVE_ENABLED="true", PROACTIVE_DB_PATH=str(tmp_path / "p.db"))
    code = (
        "import sys, proactive.worker, proactive.tasks;"
        "sys.exit(1 if 'services.clients' in sys.modules else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── Config ───────────────────────────────────────────────────────────────────


def _cfg(**env):
    from config.proactive import ProactiveSettings

    return ProactiveSettings(_env_file=None, **env)


def test_config_defaults_and_derived_values():
    c = _cfg()
    assert c.proactive_enabled is False
    assert c.deadline_offsets == [30, 7, 1]
    assert [w.label() for w in c.sweep_windows] == ["01:00-05:00/200"]
    assert c.push_available is False and c.email_available is False
    assert c.db_path(Path("/app")) == Path("/app/data/proactive.db")


@pytest.mark.parametrize(
    "key,value",
    [
        ("proactive_sweep_windows", "01:00-05:00"),
        ("proactive_sweep_windows", "25:00-05:00/10"),
        ("proactive_sweep_windows", "03:00-03:00/10"),
        ("proactive_deadline_offsets_days", "30,x"),
        ("proactive_deadline_offsets_days", ""),
        ("proactive_daily_run_time", "7am"),
        ("proactive_findings_root", "/mnt/vaults/shared"),
        ("smtp_security", "tls"),
        ("proactive_newdoc_poll_seconds", 5),
    ],
)
def test_config_rejects_bad_values(key, value):
    with pytest.raises(ValueError):
        _cfg(**{key: value})


def test_config_default_pushes_must_not_exceed_ceiling():
    with pytest.raises(ValueError):
        _cfg(proactive_max_pushes_per_week=5, proactive_max_pushes_per_week_limit=3)


def test_channel_availability_follows_config_presence():
    assert _cfg(push_vapid_public_key="p", push_vapid_private_key="k").push_available
    assert not _cfg(push_vapid_public_key="p").push_available
    half = _cfg(smtp_host="smtp", smtp_from="a@b")  # no IMAP → no replies → no email
    assert not half.email_available
    full = _cfg(smtp_host="smtp", smtp_from="a@b", imap_host="imap", imap_user="u")
    assert full.email_available


def test_window_crossing_midnight():
    from datetime import time as t

    from config.proactive import parse_windows

    (w,) = parse_windows("23:00-02:00/5")
    assert w.contains(t(23, 30)) and w.contains(t(1, 59))
    assert not w.contains(t(2, 0)) and not w.contains(t(12, 0))


def test_app_settings_ignore_proactive_keys(monkeypatch):
    """PROACTIVE_* in the shared .env must not break config/settings.py."""
    from config.settings import Settings

    monkeypatch.setenv("PROACTIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")  # left over in an older .env
    env = REPO / "tests" / "_tmp_env"
    env.write_text("PROACTIVE_ENABLED=true\nSMTP_HOST=x\n")
    try:
        Settings(_env_file=str(env))
    finally:
        env.unlink()


# ── DB ───────────────────────────────────────────────────────────────────────


def test_db_schema_and_recreate_after_delete(proactive_on):
    from proactive import db

    assert db.schema_version() == len(db.MIGRATIONS)
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "proactive_prefs", "window_log", "jobs", "schedules", "sweep_cursor",
        "newdoc_cursor", "doc_analysis", "channel_link", "link_code",
    } <= tables
    proactive_on.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(proactive_on) + suffix).unlink(missing_ok=True)
    assert db.schema_version() == len(db.MIGRATIONS)


# ── Jobs ─────────────────────────────────────────────────────────────────────


def test_failing_job_backs_off_then_fails_with_last_error(proactive_on):
    from proactive import clock, jobs

    calls = []

    @jobs.handler("t_boom")
    async def boom(job):
        calls.append(clock.utcnow())
        raise RuntimeError("kaputt")

    jid = jobs.enqueue("t_boom")
    delays = []
    for _ in range(3):
        job = jobs.claim_next()
        assert job is not None and job.id == jid
        res = asyncio.run(jobs.run_one(job, max_attempts=3, base_seconds=30))
        after = jobs.get(jid)
        if res.status == "queued":
            delays.append(clock.parse(after.run_after) - clock.utcnow())
            assert jobs.claim_next() is None  # not due before the backoff
            clock.advance(timedelta(hours=1))
    final = jobs.get(jid)
    assert final.status == "failed" and final.attempts == 3
    assert final.last_error == "RuntimeError: kaputt"
    assert len(calls) == 3
    # exponential: ~30 s, then ~60 s
    assert timedelta(seconds=25) <= delays[0] <= timedelta(seconds=30)
    assert timedelta(seconds=55) <= delays[1] <= timedelta(seconds=60)


def test_backoff_is_capped():
    from proactive.jobs import MAX_BACKOFF, backoff

    assert backoff(1, 30) == timedelta(seconds=30)
    assert backoff(4, 30) == timedelta(seconds=240)
    assert backoff(40, 30) == MAX_BACKOFF


def test_defer_does_not_spend_an_attempt(proactive_on):
    from proactive import jobs

    @jobs.handler("t_later")
    async def later(job):
        raise jobs.Defer("host asleep", seconds=120)

    jid = jobs.enqueue("t_later")
    job = jobs.claim_next()
    res = asyncio.run(jobs.run_one(job, max_attempts=1, base_seconds=30))
    j = jobs.get(jid)
    assert res.status == "deferred" and j.status == "queued" and j.attempts == 0


def test_dedupe_key_keeps_one_pending_job(proactive_on):
    from proactive import jobs

    @jobs.handler("t_noop")
    async def noop(job):
        pass

    a = jobs.enqueue("t_noop", dedupe_key="doc:1")
    assert jobs.enqueue("t_noop", dedupe_key="doc:1") is None
    job = jobs.claim_next()
    asyncio.run(jobs.run_one(job, max_attempts=3, base_seconds=1))
    assert jobs.get(a).status == "done"
    assert jobs.enqueue("t_noop", dedupe_key="doc:1") is not None  # done frees the key


def test_llm_jobs_wait_while_the_gate_is_closed(proactive_on):
    from proactive import jobs

    @jobs.handler("t_llm", needs_llm=True)
    async def llm(job):
        pass

    @jobs.handler("t_plain")
    async def plain(job):
        pass

    jobs.enqueue("t_llm")
    plain_id = jobs.enqueue("t_plain")
    got = jobs.claim_next(llm_ok=False)
    assert got.id == plain_id
    assert jobs.claim_next(llm_ok=False) is None
    assert jobs.claim_next(llm_ok=True).kind == "t_llm"


def test_running_jobs_are_requeued_after_a_crash(proactive_on):
    from proactive import jobs

    @jobs.handler("t_noop2")
    async def noop(job):
        pass

    jid = jobs.enqueue("t_noop2")
    jobs.claim_next()
    assert jobs.get(jid).status == "running"
    assert jobs.requeue_running() == 1
    assert jobs.get(jid).status == "queued"


def test_unknown_kind_fails_at_once(proactive_on):
    from proactive import jobs

    jid = jobs.enqueue("t_nobody_handles_this")
    job = jobs.claim_next()
    asyncio.run(jobs.run_one(job, max_attempts=5, base_seconds=1))
    assert jobs.get(jid).status == "failed"


# ── Scheduler + worker loop ──────────────────────────────────────────────────


def test_heartbeat_fires_once_per_minute(proactive_on, caplog):
    from proactive import clock, jobs
    from proactive.worker import Worker
    import proactive.tasks  # noqa: F401

    w = Worker(tick_seconds=60)

    async def go():
        await w.startup()
        n1 = await w.tick_once()          # run_now → fires immediately
        n2 = await w.tick_once()          # same minute → nothing
        clock.advance(timedelta(seconds=61))
        n3 = await w.tick_once()
        return n1, n2, n3

    with caplog.at_level("INFO", logger="proactive.heartbeat"):
        assert asyncio.run(go()) == (1, 0, 1)
    assert sum("heartbeat" in r.message for r in caplog.records) == 2
    assert jobs.pending_count() == 0


def test_daily_schedule_next_occurrence(monkeypatch):
    from zoneinfo import ZoneInfo

    import config.settings as cs
    from proactive.scheduler import next_occurrence

    monkeypatch.setattr(cs, "local_tz", lambda: ZoneInfo("Europe/Berlin"))
    before = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)  # 06:00 Berlin
    after = datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc)  # 07:30 Berlin
    assert next_occurrence("daily:07:00", before) == datetime(2026, 10, 26, 7, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert next_occurrence("daily:07:00", after) == datetime(2026, 10, 27, 7, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    # across the DST change (2026-10-25): still 07:00 local
    sat = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
    assert next_occurrence("daily:07:00", sat).hour == 7


def test_schedule_spec_change_reschedules_unchanged_keeps(proactive_on):
    from proactive import scheduler
    from proactive.db import connect

    scheduler.ensure("heartbeat", "every:60")
    with connect() as c:
        first = c.execute("SELECT next_run_at FROM schedules").fetchone()[0]
    scheduler.ensure("heartbeat", "every:60")
    with connect() as c:
        assert c.execute("SELECT next_run_at FROM schedules").fetchone()[0] == first
    scheduler.ensure("heartbeat", "every:3600")
    with connect() as c:
        assert c.execute("SELECT interval_spec FROM schedules").fetchone()[0] == "every:3600"


_SIGTERM_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from proactive import jobs, worker

    done = Path(sys.argv[1])

    @jobs.handler("slow")
    async def slow(job):
        print("STARTED", flush=True)
        await asyncio.sleep(1.5)
        done.write_text("finished")

    async def amain():
        w = worker.Worker(tick_seconds=60)
        loop = asyncio.get_running_loop()
        worker._install_signals(loop, w, asyncio.current_task())
        jobs.enqueue("slow")
        await w.run()

    asyncio.run(amain())
    print("EXITED", flush=True)
    """
)


def test_sigterm_finishes_the_running_job_then_exits(tmp_path):
    script = tmp_path / "w.py"
    script.write_text(_SIGTERM_SCRIPT)
    done = tmp_path / "done"
    env = dict(
        os.environ,
        PROACTIVE_ENABLED="true",
        PROACTIVE_DB_PATH=str(tmp_path / "p.db"),
        PYTHONPATH=str(REPO),
    )
    p = subprocess.Popen(
        [sys.executable, str(script), str(done)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.time() + 30
        line = ""
        while time.time() < deadline:
            line = p.stdout.readline()
            if "STARTED" in line:
                break
        assert "STARTED" in line, p.stderr.read()
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, err
    assert done.read_text() == "finished"
    assert "EXITED" in out

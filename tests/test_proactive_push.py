"""Web Push channel: devices, delivery, app events (proactive/notify/webpush.py)."""

import asyncio

import pytest

FCM = "https://fcm.googleapis.com/fcm/send/abc123"
SUB = {"endpoint": FCM, "keys": {"p256dh": "BPk", "auth": "au"}}


@pytest.fixture
def push_on(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from proactive.notify import webpush

    pub, priv = webpush.generate_keys()
    monkeypatch.setenv("PUSH_VAPID_PUBLIC_KEY", pub)
    monkeypatch.setenv("PUSH_VAPID_PRIVATE_KEY", priv)
    monkeypatch.setenv("PROACTIVE_APP_BASE_URL", "https://brain.lan")
    cfgmod.proactive_settings.cache_clear()
    return proactive_on


@pytest.fixture
def sent(monkeypatch):
    """Record pushes instead of sending; `sent.status` is what the service answers."""
    from proactive.notify import webpush

    class Calls(list):
        pass

    calls = Calls()
    calls.urgency = []

    def fake(sub, data, urgency="normal"):
        calls.append((sub["endpoint"], data))
        calls.urgency.append(urgency)
        return fake.status

    fake.status = 201
    monkeypatch.setattr(webpush, "_send_one", fake)
    calls.fake = fake
    return calls


def _drain(times=3):
    from proactive.worker import Worker
    import proactive.tasks  # noqa: F401

    w = Worker()
    for _ in range(times):
        asyncio.run(w.tick_once())


def test_generated_keys_load_in_pywebpush():
    from py_vapid import Vapid
    from proactive.notify import webpush

    pub, priv = webpush.generate_keys()
    assert Vapid.from_string(priv).private_key is not None
    import base64

    raw = base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))
    assert len(raw) == 65 and raw[0] == 4  # uncompressed P-256 point


@pytest.mark.parametrize("endpoint", [
    "https://evil.example/push",
    "http://fcm.googleapis.com/fcm/send/x",          # not https
    "https://fcm.googleapis.com.evil.example/x",
    "",
])
def test_subscribe_refuses_endpoints_outside_the_push_services(push_on, endpoint):
    from proactive.notify import webpush

    with pytest.raises(webpush.SubscriptionRefused):
        webpush.subscribe("alice", {**SUB, "endpoint": endpoint})
    assert webpush.devices("alice") == []


def test_one_endpoint_one_user_and_several_devices(push_on):
    from proactive.notify import webpush

    webpush.subscribe("alice", SUB, "Android · Chrome")
    webpush.subscribe("alice", {**SUB, "endpoint": "https://web.push.apple.com/QX"}, "iPhone · Safari")
    assert [d["device"] for d in webpush.devices("alice")] == ["Android · Chrome", "iPhone · Safari"]
    webpush.subscribe("bob", SUB, "Android · Chrome")  # same phone, other account
    assert len(webpush.devices("alice")) == 1 and len(webpush.devices("bob")) == 1


def test_push_is_a_channel_only_with_a_device_and_the_choice(push_on):
    from proactive import prefs
    from proactive.notify import chosen_channels, webpush

    p = prefs.get("alice")
    p.channels = "push"
    prefs.save(p)
    assert chosen_channels("alice") == []           # no device yet
    webpush.subscribe("alice", SUB)
    assert chosen_channels("alice") == ["push"]


def test_channel_choice_is_a_list_in_fixed_order(proactive_on):
    from proactive import prefs

    assert prefs.ProactivePrefs("a", channels="email,push").channel_list() == ["push", "email"]
    with pytest.raises(ValueError):
        prefs.save(prefs.ProactivePrefs("a", channels="sms"))


def test_reminder_reaches_the_devices_without_title_or_link_in_the_body(push_on, sent):
    from proactive import prefs
    from proactive.notify import compose, enqueue, webpush

    p = prefs.get("alice")
    p.channels, p.language = "push", "en"
    prefs.save(p)
    webpush.subscribe("alice", SUB)
    n = compose("alice", "en", finding_id="f1", title="Cancellation deadline: gym",
                urgency="critical", note="7 days left")
    assert enqueue(n) == ["push"]
    _drain()
    assert len(sent) == 1
    endpoint, data = sent[0]
    assert endpoint == FCM
    assert data["title"] == "Cancellation deadline: gym"
    assert data["url"] == "https://brain.lan/findings/f1"
    assert "7 days left" in data["body"] and "Critical" in data["body"]
    assert data["title"] not in data["body"] and "https://" not in data["body"]


def test_a_gone_device_is_removed_and_not_retried(push_on, sent):
    from proactive.notify import webpush

    webpush.subscribe("alice", SUB)
    sent.fake.status = 410
    assert asyncio.run(webpush.send("alice", webpush.payload("t", "b", "/"))) == 0
    assert webpush.devices("alice") == []


def test_a_failing_push_service_raises_for_a_retry(push_on, sent):
    from proactive.notify import webpush

    webpush.subscribe("alice", SUB)
    sent.fake.status = 500
    with pytest.raises(RuntimeError):
        asyncio.run(webpush.send("alice", webpush.payload("t", "b", "/")))
    assert len(webpush.devices("alice")) == 1


def test_app_events_go_to_push_only_and_follow_their_switch(push_on, sent):
    from proactive import linking, prefs
    from proactive.notify import enqueue_event, webpush

    p = prefs.get("alice")
    p.channels, p.language = "email,push", "en"
    prefs.save(p)
    linking.link("alice", "email", "alice@example.com")
    webpush.subscribe("alice", SUB)

    enqueue_event("alice", "sync", "/", "3 new, 0 removed")
    _drain()
    assert [d["title"] for _, d in sent] == ["Sync finished"]
    assert sent[0][1]["url"] == "https://brain.lan/"

    p = prefs.get("alice")
    p.notify_werkbank = False
    prefs.save(p)
    enqueue_event("alice", "werkbank", "/werkbank", "Compare offers")
    _drain()
    assert len(sent) == 1
    # never an email job for an event
    from proactive.db import connect

    with connect() as conn:
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM jobs")}
    assert "notify_channel" not in kinds


def test_app_events_never_raise_and_need_push_configured(proactive_on, monkeypatch):
    from proactive.notify import enqueue_event
    from proactive.db import connect

    enqueue_event("alice", "sync", "/")              # no keys: nothing queued
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_app_events_with_the_switch_off_queue_nothing(proactive_off):
    from proactive.notify import enqueue_event

    enqueue_event("alice", "werkbank", "/werkbank")  # PROACTIVE_ENABLED=false: silent


def test_subscribe_route_rejects_a_foreign_origin():
    from proactive.push_routes import _same_origin

    class R:
        def __init__(self, origin, host):
            self.headers = {"origin": origin, "host": host} if origin else {"host": host}

    assert _same_origin(R("https://brain.lan", "brain.lan"))
    assert not _same_origin(R("https://evil.example", "brain.lan"))
    assert _same_origin(R(None, "brain.lan"))


def test_findings_go_out_with_high_urgency_and_the_result_is_recorded(proactive_on, monkeypatch):
    """Urgency high: Android delivers at once instead of at its next
    maintenance window (a phone just back from airplane mode, in Doze)."""
    import asyncio

    from proactive.notify import webpush

    calls = []

    def fake(sub, data, urgency="normal"):
        calls.append(urgency)
        return 201

    from config import proactive as cfgmod

    pub, priv = webpush.generate_keys()
    monkeypatch.setenv("PUSH_VAPID_PUBLIC_KEY", pub)
    monkeypatch.setenv("PUSH_VAPID_PRIVATE_KEY", priv)
    cfgmod.proactive_settings.cache_clear()
    monkeypatch.setattr(webpush, "_send_one", fake)
    webpush.subscribe("alice", {"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "k", "auth": "a"}})
    from proactive import jobs, prefs
    from proactive.notify import compose, enqueue
    from proactive.worker import Worker
    import proactive.tasks  # noqa: F401

    p = prefs.get("alice")
    p.optin, p.channels, p.language, p.quiet_start, p.quiet_end = True, "push", "en", "00:00", "00:00"
    prefs.save(p)
    enqueue(compose("alice", "en", finding_id="f1", title="Frist", urgency="medium"))
    jobs.enqueue("push_event", "alice", {"title": "Sync finished", "url": "/"})
    asyncio.run(Worker().tick_once())
    assert sorted(calls) == ["high", "normal"]
    (dev,) = webpush.devices("alice")
    assert dev["last_status"] == 201 and dev["last_sent"]

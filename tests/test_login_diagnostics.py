"""Login failures must say which thing is wrong.

A fresh install runs with the .env.example placeholder address, so the very
first login attempt fails on configuration, not credentials. The old code
caught every exception and returned None, so "host does not resolve" and "wrong
password" produced the identical message — "Login failed. Please check your
details." — which points a new user at the one thing that was fine.
"""

import httpx
import pytest

from services import paperless as pl


# ── placeholder detection ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "http://paperless.example.lan:8000",
        "https://paperless.example.com",
        "http://changeme:8000",
        "HTTP://PAPERLESS.EXAMPLE.LAN:8000",
    ],
)
def test_placeholder_urls_are_recognised(url):
    assert pl.is_placeholder_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:8000",
        "https://paperless.myhome.lan",
        "http://localhost:8000",
        "https://docs.example-company.net",  # "example-" is not the placeholder
    ],
)
def test_real_urls_are_not_flagged(url):
    assert pl.is_placeholder_url(url) is False


# ── reasons ───────────────────────────────────────────────────────────────────


def test_unconfigured_is_reported_without_a_network_call(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not hit the network for a placeholder URL")

    monkeypatch.setattr(pl.httpx, "post", _boom)
    token, reason = pl.authenticate("http://paperless.example.lan:8000", "u", "p")
    assert token is None
    assert reason == pl.AUTH_NOT_CONFIGURED


def test_dns_or_connection_failure_is_unreachable(monkeypatch):
    def _fail(*a, **k):
        raise httpx.ConnectError("nodename nor servname provided")

    monkeypatch.setattr(pl.httpx, "post", _fail)
    assert pl.authenticate("http://box.lan:8000", "u", "p") == (
        None,
        pl.AUTH_UNREACHABLE,
    )


def test_timeout_is_unreachable_not_bad_credentials(monkeypatch):
    def _slow(*a, **k):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(pl.httpx, "post", _slow)
    assert pl.authenticate("http://box.lan:8000", "u", "p")[1] == pl.AUTH_UNREACHABLE


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_credentials_are_reported_as_such(monkeypatch, status):
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(status, json={})
    )
    assert pl.authenticate("http://box.lan:8000", "u", "bad")[1] == pl.AUTH_INVALID


@pytest.mark.parametrize("status", [404, 500, 502])
def test_other_http_errors_are_server_errors(monkeypatch, status):
    """A 404 usually means the URL is not a Paperless instance — not a password."""
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(status, text="nope")
    )
    assert pl.authenticate("http://box.lan:8000", "u", "p")[1] == pl.AUTH_SERVER_ERROR


def test_success_returns_the_token_and_no_reason(monkeypatch):
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(200, json={"token": "abc123"})
    )
    assert pl.authenticate("http://box.lan:8000", "u", "p") == ("abc123", "")


def test_200_without_a_token_is_not_a_silent_success(monkeypatch):
    """Something answered 200 but is not Paperless."""
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(200, json={"detail": "hi"})
    )
    assert pl.authenticate("http://box.lan:8000", "u", "p") == (
        None,
        pl.AUTH_SERVER_ERROR,
    )


def test_non_json_200_is_a_server_error(monkeypatch):
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(200, text="<html>login</html>")
    )
    assert pl.authenticate("http://box.lan:8000", "u", "p")[1] == pl.AUTH_SERVER_ERROR


def test_get_token_still_returns_a_bare_token(monkeypatch):
    """Kept for callers that do not care why it failed."""
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(200, json={"token": "t"})
    )
    assert pl.get_token("http://box.lan:8000", "u", "p") == "t"
    monkeypatch.setattr(
        pl.httpx, "post", lambda *a, **k: httpx.Response(401, json={})
    )
    assert pl.get_token("http://box.lan:8000", "u", "p") is None

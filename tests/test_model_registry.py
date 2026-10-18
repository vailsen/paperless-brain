"""AI models as services with models below them.

What must not break: every setting that ever stored a model *name* keeps
resolving after the switch from one entry per model to one entry per service —
the migration keeps names, the flat view keeps its shape, and a rename re-points
the choices that named the model.
"""

import json
import uuid

import pytest

from app_ui.model_picker import grouped_options, is_header
from services import model_registry as reg
from services import settings_transfer as st
from services.credential_store import load_credentials, save_credentials

LEGACY = [
    {"id": "a1", "name": "Qwen3.8:27B - MTP - Q4 (FAST)", "backend": "openai_compatible",
     "model": "qwen3.8:27b-mtp-q4_K_M", "base_url": "http://192.168.234.99:11434/v1",
     "api_key": "", "lane": "local", "enabled": True, "temperature": 0.7},
    {"id": "a2", "name": "Gemma local", "backend": "openai_compatible", "model": "gemma3",
     "base_url": "http://192.168.234.99:11434/v1/", "api_key": "", "lane": "local"},
    {"id": "b1", "name": "Kimi", "backend": "openai_compatible", "model": "moonshotai/kimi-k2",
     "base_url": "https://openrouter.ai/api/v1/chat/completions", "api_key": "sk-or",
     "lane": "api", "supports_tools": False},
    {"id": "c1", "name": "Claude", "backend": "anthropic", "model": "claude-sonnet-4-6",
     "base_url": "", "api_key": "sk-ant", "lane": "api", "effort": "high"},
]


@pytest.fixture
def user():
    return f"u-{uuid.uuid4().hex[:8]}", "tok"


# ── base URL ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,backend,expected",
    [
        ("https://openrouter.ai/api/v1/chat/completions", "openai_compatible", "https://openrouter.ai/api/v1"),
        ("https://openrouter.ai/api/v1/", "openai_compatible", "https://openrouter.ai/api/v1"),
        ("http://host:11434/v1", "openai_compatible", "http://host:11434/v1"),
        ("https://api.minimaxi.chat/anthropic/v1/messages", "anthropic", "https://api.minimaxi.chat/anthropic"),
        ("", "anthropic", ""),
    ],
)
def test_pasted_endpoint_is_cut_back_to_the_base(url, backend, expected):
    """The OpenRouter 404: the client appends /chat/completions itself."""
    assert reg.normalize_base_url(url, backend) == expected


@pytest.mark.parametrize(
    "backend,url,name",
    [
        ("openai_compatible", "https://openrouter.ai/api/v1", "OpenRouter"),
        ("openai_compatible", "http://192.168.234.99:11434/v1", "Ollama (192.168.234.99)"),
        ("anthropic", "", "Anthropic"),
        ("openai_compatible", "https://llm.example.org/v1", "llm.example.org"),
    ],
)
def test_suggested_service_names(backend, url, name):
    assert reg.suggest_provider_name(backend, url) == name


# ── migration ────────────────────────────────────────────────────────────────


def test_models_with_the_same_address_become_one_service():
    providers = reg.migrate_flat(LEGACY)
    assert [p["name"] for p in providers] == ["Ollama (192.168.234.99)", "OpenRouter", "Anthropic"]
    ollama = providers[0]
    assert [m["name"] for m in ollama["models"]] == ["Qwen3.8:27B - MTP - Q4 (FAST)", "Gemma local"]
    assert ollama["lane"] == "local" and ollama["auto_named"] is True
    assert providers[1]["base_url"] == "https://openrouter.ai/api/v1"
    # Provider fields moved up; model fields stayed with the model.
    assert "api_key" not in ollama["models"][0] and ollama["models"][0]["temperature"] == 0.7


def test_same_address_with_different_keys_stays_apart():
    two = [dict(LEGACY[2], id="x"), dict(LEGACY[2], id="y", name="Kimi 2", api_key="sk-other")]
    providers = reg.migrate_flat(two)
    assert [p["name"] for p in providers] == ["OpenRouter", "OpenRouter (2)"]


def test_flat_view_is_unchanged_by_the_migration():
    """Chat, vision and Werkbank read get_models(); it must look as before."""
    flat = reg.flatten(reg.migrate_flat(LEGACY))
    by_name = {m["name"]: m for m in flat}
    for old in LEGACY:
        new = by_name[old["name"]]
        assert new["model"] == old["model"] and new["backend"] == old["backend"]
        assert new["api_key"] == old["api_key"] and new["lane"] == old["lane"]
        assert new["base_url"] == reg.normalize_base_url(old["base_url"], old["backend"])
    assert by_name["Kimi"]["supports_tools"] is False
    assert by_name["Claude"]["effort"] == "high"


def test_first_read_migrates_and_keeps_the_old_list(user):
    username, token = user
    save_credentials(username, token, {"llm_models": LEGACY, "dream_model": "Kimi"})
    assert reg.get_by_name("Kimi", username, token)["base_url"] == "https://openrouter.ai/api/v1"
    creds = load_credentials(username, token)
    assert len(creds["llm_providers"]) == 3
    assert creds["llm_models"] == LEGACY


def test_disabled_service_disables_its_models():
    providers = reg.migrate_flat(LEGACY)
    providers[0]["enabled"] = False
    flat = reg.flatten(providers)
    assert not any(m["enabled"] for m in flat if m["provider_id"] == providers[0]["id"])


# ── names ────────────────────────────────────────────────────────────────────


def test_display_names_are_unique_across_services():
    providers = reg.migrate_flat(LEGACY)
    assert reg.name_taken(providers, "kimi")
    assert not reg.name_taken(providers, "Kimi", except_model_id="b1")
    assert reg.unique_name(providers, "Kimi", "OpenRouter") == "Kimi (OpenRouter)"


def test_rename_repoints_memo_and_dream_choices(user):
    username, token = user
    save_credentials(username, token, {"memo_model": "Kimi", "dream_model": "Kimi"})
    reg.rename_references(username, token, "Kimi", "Kimi K2")
    creds = load_credentials(username, token)
    assert creds["memo_model"] == creds["dream_model"] == "Kimi K2"


# ── picker ───────────────────────────────────────────────────────────────────


def test_picker_groups_models_under_their_service():
    flat = reg.flatten(reg.migrate_flat(LEGACY))
    opts = grouped_options(flat)
    labels = list(opts.values())
    assert labels == [
        "​Ollama (192.168.234.99)", "Qwen3.8:27B - MTP - Q4 (FAST)", "Gemma local",
        "​OpenRouter", "Kimi",
        "​Anthropic", "Claude",
    ]
    headers = [k for k in opts if is_header(k)]
    assert len(headers) == 3 and not any(is_header(m["name"]) for m in flat)


def test_picker_leaves_out_disabled_models():
    flat = reg.flatten(reg.migrate_flat(LEGACY))
    flat[1]["enabled"] = False
    assert "Gemma local" not in grouped_options(flat)


# ── settings export / import ─────────────────────────────────────────────────


def test_export_carries_services_without_keys(user):
    username, token = user
    save_credentials(username, token, {"llm_models": LEGACY})
    payload = json.loads(st.build_export(username, token, language="de", theme="dark"))
    assert "llm_models" not in payload
    keys = [p["api_key"] for p in payload["llm_providers"]]
    assert "sk-or" not in keys and "sk-ant" not in keys and st.REDACTED in keys


def test_import_of_an_old_flat_export_becomes_services(user):
    username, token = user
    old_export = {"_app": st.APP_ID, "_schema": 1, "llm_models": LEGACY}
    st.apply_import(username, token, old_export, ["de", "en"])
    names = [m["name"] for m in reg.get_models(username, token)]
    assert names == [m["name"] for m in LEGACY]


def test_redacted_key_keeps_the_stored_one(user):
    username, token = user
    save_credentials(username, token, {"llm_providers": reg.migrate_flat(LEGACY)})
    exported = json.loads(st.build_export(username, token, language="de", theme="dark"))
    st.apply_import(username, token, exported, ["de", "en"])
    by_name = {m["name"]: m for m in reg.get_models(username, token)}
    assert by_name["Kimi"]["api_key"] == "sk-or" and by_name["Claude"]["api_key"] == "sk-ant"

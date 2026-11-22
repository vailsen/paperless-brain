"""The built-in WoL / idle shutdown is gone; its settings warn once, never crash."""

import logging


def test_leftover_power_settings_warn_once_and_do_not_crash(tmp_path, monkeypatch, caplog):
    from config import deprecations

    env = tmp_path / ".env"
    env.write_text("OLLAMA_SERVER=http://proxy:11434\nOLLAMA_SSH_USER=\nSHUTDOWN_PASSWORD='x'\n")
    monkeypatch.setenv("PROACTIVE_WOL_AUTO", "true")
    monkeypatch.delenv("OLLAMA_HOST_LAN_MAC_ADDRESS_WOL", raising=False)
    assert deprecations.set_removed_power_settings(str(env)) == ["SHUTDOWN_PASSWORD", "PROACTIVE_WOL_AUTO"]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(deprecations, "_warned", False)
    with caplog.at_level(logging.WARNING):
        deprecations.warn_removed_settings_once()
        deprecations.warn_removed_settings_once()
    warnings = [r for r in caplog.records if "no longer do anything" in r.getMessage()]
    assert len(warnings) == 1 and "SHUTDOWN_PASSWORD" in warnings[0].getMessage()


def test_the_settings_load_with_leftover_power_values(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("OLLAMA_HOST_LAN_MAC_ADDRESS_WOL", "aa:bb:cc:dd:ee:ff")
    monkeypatch.setenv("OLLAMA_IDLE_SHUTDOWN_MINUTES", "30")
    s = Settings()
    assert not hasattr(s, "ollama_host_lan_mac_address_wol")

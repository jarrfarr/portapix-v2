from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from eventhub.core.config import (
    AppConfig,
    ConfigError,
    HotspotConfig,
    config_yaml,
    default_config,
    dump_config,
    load_config,
    merge_config,
    save_config,
)


def test_default_config_generates_strong_secrets() -> None:
    config = default_config()
    assert len(config.hotspot.password.get_secret_value()) >= 16
    assert len(config.dashboard.secret_key.get_secret_value()) >= 32
    assert config.hotspot.password.get_secret_value() != "EventHub-Recovery"
    assert config.hotspot.enabled is True


def test_two_default_configs_have_different_secrets() -> None:
    first = default_config()
    second = default_config()
    assert first.hotspot.password.get_secret_value() != second.hotspot.password.get_secret_value()
    assert (
        first.dashboard.secret_key.get_secret_value()
        != second.dashboard.secret_key.get_secret_value()
    )


def test_config_roundtrip(tmp_path: Path) -> None:
    config = default_config()
    config = config.model_copy(update={"hostname": "shoothub"})
    target = tmp_path / "etc" / "config.yaml"
    save_config(config, target)
    assert target.stat().st_mode & 0o777 == 0o600
    loaded = load_config(target)
    assert loaded.hostname == "shoothub"
    assert loaded.hotspot.password.get_secret_value() == config.hotspot.password.get_secret_value()
    assert loaded.events_root == loaded.data_root / "events"


def test_dump_config_masks_secrets() -> None:
    config = default_config()
    public = dump_config(config, reveal_secrets=False)
    assert public["hotspot"]["password"] == "********"
    assert public["dashboard"]["secret_key"] == "********"
    revealed = dump_config(config, reveal_secrets=True)
    assert revealed["hotspot"]["password"] == config.hotspot.password.get_secret_value()


def test_config_yaml_is_valid_yaml_and_parses(tmp_path: Path) -> None:
    config = default_config()
    target = tmp_path / "config.yaml"
    target.write_text(config_yaml(config), encoding="utf-8")
    loaded = load_config(target)
    assert loaded.model_dump().keys() == config.model_dump().keys()


def test_invalid_ssid_rejected() -> None:
    with pytest.raises(ValidationError):
        HotspotConfig(ssid="x" * 33)


def test_invalid_wpa_password_rejected() -> None:
    with pytest.raises(ValidationError):
        HotspotConfig(password=SecretStr("short"))
    with pytest.raises(ValidationError):
        HotspotConfig(password=SecretStr("x" * 65))


def test_dhcp_range_must_be_inside_subnet() -> None:
    with pytest.raises(ValidationError):
        HotspotConfig(
            password=SecretStr("valid-password"),
            subnet="10.42.0.0/24",
            dhcp_start="10.43.0.10",
            dhcp_end="10.43.0.20",
        )


def test_hotspot_can_be_disabled_without_password() -> None:
    section = HotspotConfig(enabled=False, password=SecretStr(""))
    assert section.enabled is False


def test_warn_threshold_below_critical() -> None:
    with pytest.raises(ValidationError):
        AppConfig(storage={"warn_percent": 95, "critical_percent": 90})


def test_merge_config_deep_merges_and_validates() -> None:
    config = default_config()
    merged = merge_config(
        config, {"hotspot": {"ssid": "NewName"}, "discovery": {"hostname": "hub"}}
    )
    assert merged.hotspot.ssid == "NewName"
    assert merged.discovery.hostname == "hub"
    assert merged.hotspot.password == config.hotspot.password
    with pytest.raises(ConfigError):
        merge_config(config, {"hotspot": {"ssid": ""}})


def test_load_missing_config_raises() -> None:
    with pytest.raises(ConfigError):
        load_config("/nonexistent/eventhub/config.yaml")


def test_load_invalid_yaml_raises(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("hotspot: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(target)


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text(
        "hotspot:\n  ssid: Hub\n  password: valid-password\n  typo_key: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(target)


def test_auto_interface_roles_allowed() -> None:
    config = AppConfig(roles={"ap": "auto", "wan": "auto", "lan": "auto", "airdrop": "auto"})
    assert config.roles.ap == "auto"


def test_same_ap_and_airdrop_interface_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig(roles={"ap": "wlan0", "airdrop": "wlan0"})

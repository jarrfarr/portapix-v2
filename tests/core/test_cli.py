from __future__ import annotations

import json
from pathlib import Path

from tests.support import make_config
from typer.testing import CliRunner

from eventhub.cli import app
from eventhub.core.config import save_config

runner = CliRunner()


def invoke(config_path: Path, *args: str) -> object:
    return runner.invoke(app, ["--config", str(config_path), *args])


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "EventHub" in result.output


def test_status_json(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config_path = tmp_path / "etc" / "config.yaml"
    save_config(config, config_path)
    result = invoke(config_path, "status", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "hotspot" in payload
    assert "network" in payload


def test_modules_lists_catalog(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config_path = tmp_path / "etc" / "config.yaml"
    save_config(config, config_path)
    result = invoke(config_path, "modules")
    assert result.exit_code == 0, result.output
    assert "hotspot" in result.output
    assert "dashboard" in result.output


def test_config_validate(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config_path = tmp_path / "etc" / "config.yaml"
    save_config(config, config_path)
    result = invoke(config_path, "config", "validate")
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_config_get_masks_secrets(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config_path = tmp_path / "etc" / "config.yaml"
    save_config(config, config_path)
    result = invoke(config_path, "config", "get", "hotspot.password")
    assert result.exit_code == 0
    assert "********" in result.output


def test_config_get_unknown_key(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config_path = tmp_path / "etc" / "config.yaml"
    save_config(config, config_path)
    result = invoke(config_path, "config", "get", "nope.nope")
    assert result.exit_code == 2


def test_apply_dry_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config_path = tmp_path / "etc" / "config.yaml"
    save_config(config, config_path)
    result = invoke(config_path, "--dry-run", "apply")
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output


def test_missing_config_status_does_not_crash(tmp_path: Path) -> None:
    result = invoke(tmp_path / "missing.yaml", "status", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["hotspot"]["installed"] is False

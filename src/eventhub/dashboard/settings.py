"""Dashboard runtime settings rendered by the dashboard module.

The unprivileged dashboard cannot read the root-only main config, so the
dashboard module renders a small dedicated JSON file owned by
``root:eventhub-web``.  It contains the session secret, bind settings, helper
socket and state-db path, nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field, SecretStr, field_validator

from eventhub.core.config import AppConfig, StrictModel


class DashboardSettings(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=80, ge=1, le=65535)
    secret_key: SecretStr
    session_hours: int = Field(default=12, ge=1)
    cookie_name: str = "eventhub_session"
    cookie_secure: bool = False
    login_rate_limit: int = Field(default=8, ge=1)
    login_rate_window: int = Field(default=300, ge=30)
    helper_socket: Path = Path("/run/eventhub/helper.sock")
    db_path: Path = Path("/var/lib/eventhub/eventhub.db")
    log_dir: Path = Path("/var/log/eventhub")
    config_path: Path = Path("/etc/eventhub/config.yaml")
    hostname: str = "eventhub"
    domain: str = "local"
    hotspot_server_ip: str = "10.42.0.1"
    trusted_hosts: list[str] = Field(default_factory=list)
    first_boot_password_file: Path = Path("/var/lib/eventhub/first-boot-admin.txt")

    @field_validator(
        "helper_socket",
        "db_path",
        "log_dir",
        "config_path",
        "first_boot_password_file",
    )
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("dashboard settings paths must be absolute")
        return value

    @classmethod
    def from_config(cls, config: AppConfig) -> DashboardSettings:
        cfg = config.dashboard
        hosts = list(cfg.trusted_hosts)
        for candidate in (
            f"{config.discovery.hostname}.{config.discovery.domain}",
            config.discovery.hostname,
            config.hotspot.server_ip,
        ):
            if candidate not in hosts:
                hosts.append(candidate)
        return cls(
            host=cfg.host,
            port=cfg.port,
            secret_key=cfg.secret_key,
            session_hours=cfg.session_hours,
            cookie_name=cfg.cookie_name,
            cookie_secure=cfg.cookie_secure,
            login_rate_limit=cfg.login_rate_limit,
            login_rate_window=cfg.login_rate_window,
            helper_socket=config.helper.socket_path,
            db_path=config.db_path,
            log_dir=config.log_dir,
            config_path=config.install.config_path,
            hostname=config.discovery.hostname,
            domain=config.discovery.domain,
            hotspot_server_ip=config.hotspot.server_ip,
            trusted_hosts=hosts,
            first_boot_password_file=cfg.first_boot_password_file,
        )

    def to_json(self) -> str:
        payload = self.model_dump()
        payload["secret_key"] = self.secret_key.get_secret_value()
        for key in (
            "helper_socket",
            "db_path",
            "log_dir",
            "config_path",
            "first_boot_password_file",
        ):
            payload[key] = str(payload[key])
        return json.dumps(payload, indent=2) + "\n"

    @classmethod
    def load(cls, path: Path | str) -> DashboardSettings:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

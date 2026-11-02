"""Typed, validated configuration for the whole appliance.

There is exactly one source of truth (``/etc/eventhub/config.yaml`` by
default).  Every module owns a section of the top-level document and can be
disabled without making the rest of the configuration invalid.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from eventhub.core.errors import ConfigError

DEFAULT_CONFIG_PATH = Path(os.environ.get("EVENTHUB_CONFIG", "/etc/eventhub/config.yaml"))
DEFAULT_DATA_ROOT = Path(os.environ.get("EVENTHUB_DATA_ROOT", "/srv/eventhub"))
DEFAULT_STATE_DIR = Path(os.environ.get("EVENTHUB_STATE_DIR", "/var/lib/eventhub"))

_HOSTNAME_RE = re.compile(r"^(?!-)[a-zA-Z0-9-]{1,63}(?<!-)$")
_IFACE_RE = re.compile(r"^[a-zA-Z0-9_.:@-]{1,15}$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def generate_secret(length: int = 32) -> str:
    """Generate a URL-safe secret with at least ``length`` characters."""

    value = secrets.token_urlsafe(length)
    while len(value) < length:
        value += secrets.token_urlsafe(length)
    return value[: max(length, 32)]


def generate_wifi_password() -> str:
    """Generate a strong WPA2/WPA3 passphrase (no ambiguous characters)."""

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(24))


class StrictModel(BaseModel):
    """Base model: reject unknown keys so typos are caught, not ignored."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


def _check_absolute(value: Path, field: str) -> Path:
    if not value.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return value


class InterfaceRoles(StrictModel):
    """Role-to-interface map.  ``auto`` lets the network module detect it."""

    ap: str = "auto"
    airdrop: str = "auto"
    wan: str = "auto"
    lan: str = "auto"

    @field_validator("ap", "airdrop", "wan", "lan")
    @classmethod
    def _valid_interface(cls, value: str) -> str:
        if value in {"auto", "none"}:
            return value
        if not _IFACE_RE.match(value):
            raise ValueError(f"invalid interface name: {value!r}")
        return value


class HotspotConfig(StrictModel):
    enabled: bool = True
    ssid: str = "EventHub"
    password: SecretStr = Field(default_factory=lambda: SecretStr(generate_wifi_password()))
    country_code: str = "GB"
    band: Literal["2.4", "5", "auto"] = "auto"
    channel: int = 0
    hw_mode: Literal["a", "g", "auto"] = "auto"
    hidden: bool = False
    auth_mode: Literal["wpa2", "wpa3", "wpa2-wpa3"] = "wpa2-wpa3"
    interface: str = "auto"
    subnet: str = "10.42.0.0/24"
    dhcp_start: str = "10.42.0.10"
    dhcp_end: str = "10.42.0.200"
    lease_time: str = "12h"
    dns_domain: str = "eventhub.local"
    max_clients: int = Field(default=64, ge=1, le=512)
    client_isolation: bool = False
    ieee80211d: bool = True

    @field_validator("ssid")
    @classmethod
    def _valid_ssid(cls, value: str) -> str:
        encoded = value.encode("utf-8")
        if not 1 <= len(encoded) <= 32:
            raise ValueError("SSID must be 1-32 bytes")
        if any(ord(c) < 32 for c in value):
            raise ValueError("SSID must not contain control characters")
        return value

    @field_validator("country_code")
    @classmethod
    def _valid_country(cls, value: str) -> str:
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("country_code must be a two-letter ISO code")
        return value

    @field_validator("subnet")
    @classmethod
    def _valid_subnet(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid subnet: {value}") from exc
        if network.version != 4:
            raise ValueError("only IPv4 hotspot subnets are supported")
        if not 16 <= network.prefixlen <= 30:
            raise ValueError("hotspot subnet must be between /16 and /30")
        return str(network)

    @field_validator("password")
    @classmethod
    def _valid_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            return value
        if 8 <= len(raw) <= 63:
            return value
        if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
            return value
        raise ValueError("WPA passphrase must be 8-63 characters (or 64 hex digits)")

    @model_validator(mode="after")
    def _check_hotspot(self) -> HotspotConfig:
        if self.enabled and not self.password.get_secret_value():
            raise ValueError("hotspot.password is required when the hotspot is enabled")
        try:
            network = ipaddress.ip_network(self.subnet, strict=False)
            start = ipaddress.ip_address(self.dhcp_start)
            end = ipaddress.ip_address(self.dhcp_end)
        except ValueError as exc:
            raise ValueError(f"invalid DHCP range: {exc}") from exc
        if start not in network or end not in network:
            raise ValueError("DHCP range must be inside the hotspot subnet")
        if int(start) >= int(end):
            raise ValueError("DHCP range start must be before end")
        if start in {network.network_address, network.broadcast_address}:
            raise ValueError("DHCP range start must be a usable host address")
        if self.band == "2.4" and self.channel not in (0, *range(1, 15)):
            raise ValueError("2.4 GHz channel must be 0 (auto) or 1-14")
        if self.band == "5" and self.channel != 0 and not 32 <= self.channel <= 196:
            raise ValueError("5 GHz channel must be 0 (auto) or 32-196")
        if self.hw_mode == "g" and self.band == "5":
            raise ValueError("hw_mode 'g' is incompatible with the 5 GHz band")
        return self

    @property
    def network(self) -> ipaddress.IPv4Network:
        network = ipaddress.ip_network(self.subnet, strict=False)
        assert isinstance(network, ipaddress.IPv4Network)
        return network

    @property
    def server_ip(self) -> str:
        return str(self.network.network_address + 1)


class WifiCredential(StrictModel):
    ssid: str
    password: SecretStr = SecretStr("")
    priority: int = 0
    hidden: bool = False

    @field_validator("ssid")
    @classmethod
    def _ssid_len(cls, value: str) -> str:
        if not 1 <= len(value.encode("utf-8")) <= 32:
            raise ValueError("wifi SSID must be 1-32 bytes")
        return value

    @field_validator("password")
    @classmethod
    def _pw_len(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and not (8 <= len(raw) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", raw)):
            raise ValueError("wifi password must be empty, 8-63 chars, or 64 hex digits")
        return value


class NetworkConfig(StrictModel):
    enabled: bool = True
    uplink: Literal["auto", "wan", "wifi", "none"] = "auto"
    internet_sharing: bool = True
    wifi_client_interface: str = "auto"
    wifi_networks: list[WifiCredential] = Field(default_factory=list)
    connectivity_urls: list[str] = Field(
        default_factory=lambda: [
            "http://connectivitycheck.gstatic.com/generate_204",
            "http://deb.debian.org/",
        ]
    )
    connectivity_timeout: float = Field(default=4.0, gt=0, le=30)
    connectivity_interval: int = Field(default=30, ge=5)
    dns_servers: list[str] = Field(default_factory=lambda: ["1.1.1.1", "9.9.9.9"])
    nft_table: str = "eventhub"
    forward_wan_lan: bool = False
    kick_clients_on_apply: bool = False
    interface_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("wifi_client_interface")
    @classmethod
    def _wifi_iface(cls, value: str) -> str:
        if value not in {"auto", "none"} and not _IFACE_RE.match(value):
            raise ValueError("invalid wifi_client_interface")
        return value

    @field_validator("nft_table")
    @classmethod
    def _nft_table(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,30}", value):
            raise ValueError("nft_table must be a simple nftables table name")
        return value

    @field_validator("dns_servers")
    @classmethod
    def _valid_dns(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValueError(f"invalid DNS server: {value}") from exc
        return values

    @field_validator("connectivity_urls")
    @classmethod
    def _valid_urls(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("http://", "https://")):
                raise ValueError(f"connectivity URL must be http(s): {value}")
        return values


class DriveConfig(StrictModel):
    """An operator-chosen storage drive.  Empty values mean "auto"."""

    uuid: str = ""
    label: str = ""
    device: str = ""
    fstype: Literal["auto", "ext4", "exfat", "vfat", "xfs", "btrfs"] = "auto"
    mount_point: Path = Path("/media/eventhub/data")
    options: str = "noatime,nofail,x-systemd.device-timeout=10s"
    auto_mount: bool = True

    @field_validator("mount_point")
    @classmethod
    def _mount_point_absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "storage.drive.mount_point")


class StorageConfig(StrictModel):
    enabled: bool = True
    required: bool = False
    mount_root: Path = Path("/media/eventhub")
    drive: DriveConfig = Field(default_factory=DriveConfig)
    warn_percent: int = Field(default=80, ge=1, le=99)
    critical_percent: int = Field(default=90, ge=1, le=100)
    allow_format: bool = True
    format_requires_confirmation: str = "FORMAT"
    minimum_free_gb: int = Field(default=2, ge=0)
    checksum_on_ingest: bool = False
    duplicate_detection: bool = False
    eject_grace_seconds: int = Field(default=5, ge=0, le=120)

    @field_validator("mount_root")
    @classmethod
    def _mount_root_absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "storage.mount_root")

    @model_validator(mode="after")
    def _thresholds(self) -> StorageConfig:
        if self.warn_percent >= self.critical_percent:
            raise ValueError("storage.warn_percent must be below critical_percent")
        return self


class UsersConfig(StrictModel):
    enabled: bool = True
    default_role: Literal["viewer", "shooter", "editor"] = "viewer"
    min_password_length: int = Field(default=12, ge=8, le=128)
    guest_default_hours: int = Field(default=24, ge=1, le=24 * 30)
    system_user_prefix: str = "eh_"
    allow_guest_accounts: bool = True
    default_group: str = "eventhub-users"

    @field_validator("system_user_prefix")
    @classmethod
    def _valid_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,10}", value):
            raise ValueError("system_user_prefix must be a valid unix name prefix")
        return value


class EventsConfig(StrictModel):
    enabled: bool = True
    root: Path | None = None
    active_event: str | None = None
    layout: list[str] = Field(
        default_factory=lambda: ["ingest/{user}", "raw", "edit", "exports", "_eventhub"]
    )
    archive_is_read_only: bool = True
    auto_activate_new_event: bool = True
    keep_metadata: bool = True
    default_ingest_user: str = "anonymous"
    slug_max_length: int = Field(default=48, ge=8, le=64)

    @field_validator("root")
    @classmethod
    def _root_absolute(cls, value: Path | None) -> Path | None:
        if value is not None:
            return _check_absolute(value, "events.root")
        return value

    @field_validator("active_event")
    @classmethod
    def _active_slug(cls, value: str | None) -> str | None:
        if value is not None and not _SLUG_RE.match(value):
            raise ValueError("events.active_event must be a valid slug")
        return value

    @field_validator("layout")
    @classmethod
    def _valid_layout(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"event layout path must be relative and safe: {value}")
        return values


class CopypartyConfig(StrictModel):
    enabled: bool = True
    port: int = Field(default=3923, ge=1, le=65535)
    bind: list[str] = Field(default_factory=lambda: ["0.0.0.0"])
    base_url: str = "/"
    config_path: Path = Path("/etc/eventhub/copyparty.conf")
    home_dir: Path = Path("/var/lib/eventhub/copyparty")
    password_salt: SecretStr = Field(default_factory=lambda: SecretStr(generate_secret(24)))
    thumbnails: Literal["auto", "pyvips", "pil", "off"] = "auto"
    thumbnail_age_days: int = Field(default=30, ge=1, le=3650)
    max_upload_mb: int = Field(default=0, ge=0)
    allow_delete: bool = True
    require_auth_for_read: bool = False
    idle_timeout: int = Field(default=3600, ge=60)
    extra_args: list[str] = Field(default_factory=list)
    extra_volumes: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("config_path", "home_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "copyparty path")

    @field_validator("password_salt")
    @classmethod
    def _salt(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("copyparty.password_salt must be at least 16 characters")
        return value

    @field_validator("extra_args")
    @classmethod
    def _no_config_override(cls, values: list[str]) -> list[str]:
        for value in values:
            if value in {"-c", "--config"} or value.startswith(("-c=", "--config=")):
                raise ValueError("extra_args must not override the generated config (-c)")
        return values


class SmbConfig(StrictModel):
    enabled: bool = True
    workgroup: str = "EVENTHUB"
    server_string: str = "EventHub"
    config_path: Path = Path("/etc/samba/smb.conf")
    min_protocol: Literal["SMB2", "SMB2_02", "SMB2_10", "SMB3", "SMB3_00"] = "SMB2"
    max_protocol: Literal["SMB3", "SMB3_00", "SMB3_02", "SMB3_11"] = "SMB3_11"
    socket_options: str = "TCP_NODELAY IPTOS_LOWDELAY SO_RCVBUF=131072 SO_SNDBUF=131072"
    aio_read_size: int = Field(default=131072, ge=4096)
    aio_write_size: int = Field(default=131072, ge=4096)
    use_sendfile: bool = True
    strict_sync: bool = False
    veto_files: list[str] = Field(
        default_factory=lambda: [".DS_Store", "._*", "Thumbs.db", "desktop.ini", ".Spotlight-V100"]
    )
    guest_ok: bool = False
    hide_dot_files: bool = True

    @field_validator("config_path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "smb.config_path")


class AirdropConfig(StrictModel):
    enabled: bool = True
    device_name: str = "EventHub"
    interface: str = "auto"
    receive_mode: Literal["everyone", "contacts"] = "everyone"
    target_follows_active_event: bool = True
    target_dir: Path | None = Path("/srv/eventhub/inbox")
    owl_dir: Path = Path("/opt/omdrop-owl")
    owl_repo: str = "https://github.com/t4t5/omdrop-owl"
    owl_ref: str = "main"
    owl_user: str = "eventhub-airdrop"
    build_on_install: bool = True
    require_monitor_mode: bool = True
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".jpg",
            ".jpeg",
            ".png",
            ".heic",
            ".heif",
            ".dng",
            ".cr2",
            ".cr3",
            ".nef",
            ".arw",
            ".raf",
            ".orf",
            ".rw2",
            ".mov",
            ".mp4",
            ".m4v",
            ".avi",
            ".mkv",
            ".mts",
            ".m2ts",
        ]
    )
    max_file_gb: int = Field(default=64, ge=1)

    @field_validator("target_dir", "owl_dir")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        if value is not None:
            return _check_absolute(value, "airdrop path")
        return value

    @field_validator("interface")
    @classmethod
    def _iface(cls, value: str) -> str:
        if value not in {"auto", "none"} and not _IFACE_RE.match(value):
            raise ValueError("invalid airdrop interface")
        return value


class DiscoveryConfig(StrictModel):
    enabled: bool = True
    hostname: str = "eventhub"
    domain: str = "local"
    advertise_smb: bool = True
    advertise_copyparty: bool = True
    advertise_dashboard: bool = True
    copyparty_port: int = Field(default=3923, ge=1, le=65535)
    dashboard_port: int = Field(default=80, ge=1, le=65535)
    interfaces: list[str] = Field(default_factory=list)
    publish_ipv4: bool = True
    publish_ipv6: bool = False

    @field_validator("hostname")
    @classmethod
    def _hostname(cls, value: str) -> str:
        if not _HOSTNAME_RE.match(value) or len(value) > 63:
            raise ValueError("invalid mDNS hostname")
        return value.lower()

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        if value != "local" and not _HOSTNAME_RE.match(value):
            raise ValueError("invalid mDNS domain")
        return value


class DashboardConfig(StrictModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = Field(default=80, ge=1, le=65535)
    secret_key: SecretStr = Field(default_factory=lambda: SecretStr(generate_secret(48)))
    session_hours: int = Field(default=12, ge=1, le=24 * 30)
    cookie_name: str = "eventhub_session"
    cookie_secure: bool = False
    login_rate_limit: int = Field(default=8, ge=1, le=100)
    login_rate_window: int = Field(default=300, ge=30)
    max_upload_mb: int = Field(default=0, ge=0)
    bind_interfaces_only: bool = True
    trusted_hosts: list[str] = Field(
        default_factory=lambda: ["eventhub.local", "localhost", "127.0.0.1", "10.42.0.1"]
    )
    first_boot_password_file: Path = Path("/var/lib/eventhub/first-boot-admin.txt")
    log_dir: Path = Path("/var/log/eventhub")

    @field_validator("first_boot_password_file", "log_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "dashboard path")

    @model_validator(mode="after")
    def _secret(self) -> DashboardConfig:
        if (
            self.enabled
            and self.secret_key.get_secret_value()
            and len(self.secret_key.get_secret_value()) < 32
        ):
            raise ValueError("dashboard.secret_key must be at least 32 characters")
        return self

class HelperConfig(StrictModel):
    enabled: bool = True
    socket_path: Path = Path("/run/eventhub/helper.sock")
    socket_group: str = "eventhub"
    socket_mode: int = Field(default=0o660, ge=0, le=0o777)
    request_timeout: float = Field(default=120.0, gt=0)
    max_request_bytes: int = Field(default=1_048_576, ge=4096)
    allowed_actions: list[str] = Field(
        default_factory=lambda: [
            "apply",
            "status",
            "service.start",
            "service.stop",
            "service.restart",
            "module.enable",
            "module.disable",
            "user.create",
            "user.update",
            "user.delete",
            "event.create",
            "event.activate",
            "event.archive",
            "storage.mount",
            "storage.unmount",
            "storage.format",
            "network.scan",
            "network.connect",
            "network.test",
            "hotspot.kick",
            "system.reboot",
            "system.shutdown",
            "system.export",
            "system.import",
            "system.update",
            "selftest.run",
            "recovery.admin_password",
            "recovery.hotspot",
            "recovery.detect",
        ]
    )

    @field_validator("socket_path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "helper.socket_path")


class SystemConfig(StrictModel):
    enabled: bool = True
    journald_max_use: str = "128M"
    journald_keep_free: str = "512M"
    journald_max_file_size: str = "16M"
    throttle_monitor: bool = True
    power_warning: bool = True
    overlay_root: bool = False
    swap_size_mb: int = Field(default=1024, ge=0)
    boot_timeout: int = Field(default=90, ge=10)
    update_repo: str = "https://github.com/OWNER/eventhub"
    update_branch: str = "main"
    update_check: bool = True
    automatic_reboot: bool = False
    log_retention_days: int = Field(default=14, ge=1)
    metrics_interval: int = Field(default=10, ge=1)
    trusted_timezone: bool = True


class InstallConfig(StrictModel):
    repo_url: str = "https://github.com/OWNER/eventhub"
    branch: str = "main"
    install_root: Path = Path("/opt/eventhub")
    venv_path: Path = Path("/opt/eventhub/venv")
    config_path: Path = Path("/etc/eventhub/config.yaml")
    state_dir: Path = Path("/var/lib/eventhub")
    data_root: Path = Path("/srv/eventhub")
    log_file: Path = Path("/var/log/eventhub-install.log")
    service_user: str = "eventhub"
    service_group: str = "eventhub"
    dashboard_user: str = "eventhub-web"
    airdrop_user: str = "eventhub-airdrop"
    admin_password_file: Path = Path("/var/lib/eventhub/first-boot-admin.txt")

    @field_validator(
        "install_root",
        "venv_path",
        "config_path",
        "state_dir",
        "data_root",
        "log_file",
        "admin_password_file",
    )
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "install path")


class AppConfig(StrictModel):
    """The single configuration document."""

    version: int = 1
    hostname: str = "eventhub"
    timezone: str = "Etc/UTC"
    data_root: Path = DEFAULT_DATA_ROOT
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = Path("/var/log/eventhub")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    roles: InterfaceRoles = Field(default_factory=InterfaceRoles)
    hotspot: HotspotConfig = Field(default_factory=HotspotConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    users: UsersConfig = Field(default_factory=UsersConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    fileserver_copyparty: CopypartyConfig = Field(default_factory=CopypartyConfig)
    fileserver_smb: SmbConfig = Field(default_factory=SmbConfig)
    airdrop: AirdropConfig = Field(default_factory=AirdropConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    helper: HelperConfig = Field(default_factory=HelperConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    install: InstallConfig = Field(default_factory=InstallConfig)

    @field_validator("hostname")
    @classmethod
    def _hostname(cls, value: str) -> str:
        if not _HOSTNAME_RE.match(value):
            raise ValueError("hostname must contain only letters, digits and dashes")
        return value.lower()

    @field_validator("data_root", "state_dir", "log_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _check_absolute(value, "path")

    @model_validator(mode="after")
    def _cross_section(self) -> AppConfig:
        if self.events.enabled:
            _check_absolute(self.events_root, "events.root")
        if (
            self.roles.ap not in {"auto", "none"}
            and self.roles.airdrop not in {"auto", "none"}
            and self.roles.ap == self.roles.airdrop
        ):
            raise ValueError("the AP and AirDrop interfaces must differ")
        if not self.discovery.hostname:
            raise ValueError("discovery.hostname must not be empty")
        return self

    @property
    def events_root(self) -> Path:
        if self.events.root is not None:
            return self.events.root
        if self.storage.enabled and self.storage.drive.uuid:
            return self.storage.drive.mount_point / "events"
        return self.data_root / "events"

    @property
    def ingest_root(self) -> Path:
        return self.data_root / "inbox"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "eventhub.db"

    def section(self, name: str) -> StrictModel:
        """Return a module config section, raising a clear error if absent."""

        value = getattr(self, name, None)
        if not isinstance(value, StrictModel):
            raise ConfigError(f"unknown configuration section: {name}")
        return value

    def module_enabled(self, name: str) -> bool:
        try:
            section = self.section(name)
        except ConfigError:
            return False
        return bool(getattr(section, "enabled", False))

    def public_dict(self) -> dict[str, Any]:
        """Dump config without revealing secrets (safe for the dashboard)."""

        return dump_config(self, reveal_secrets=False)

    def to_dict(self) -> dict[str, Any]:
        return dump_config(self, reveal_secrets=True)


def _to_plain(value: Any, *, reveal_secrets: bool) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value() if reveal_secrets else "********"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return {
            key: _to_plain(item, reveal_secrets=reveal_secrets)
            for key, item in value.model_dump().items()
        }
    if isinstance(value, Mapping):
        return {
            str(key): _to_plain(item, reveal_secrets=reveal_secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_to_plain(item, reveal_secrets=reveal_secrets) for item in value]
    return value


def dump_config(config: AppConfig, *, reveal_secrets: bool = True) -> dict[str, Any]:
    """Return a YAML/JSON-friendly dict, optionally masking secrets."""

    value = _to_plain(config, reveal_secrets=reveal_secrets)
    if not isinstance(value, dict):
        raise ConfigError("internal error: config did not serialise to a mapping")
    return value


def default_config() -> AppConfig:
    """Create a fresh config with strong generated secrets."""

    config = AppConfig(
        hotspot=HotspotConfig(
            ssid="EventHub",
            password=SecretStr(generate_wifi_password()),
        ),
        dashboard=DashboardConfig(
            secret_key=SecretStr(generate_secret(48)),
        ),
        airdrop=AirdropConfig(device_name="EventHub"),
    )
    return config


def config_yaml(config: AppConfig) -> str:
    return yaml.safe_dump(
        dump_config(config, reveal_secrets=True),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def save_config(
    config: AppConfig,
    path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    mode: int = 0o600,
) -> Path:
    """Atomically write config, creating the parent directory if needed."""

    target = Path(path)
    if not target.is_absolute():
        raise ConfigError("config path must be absolute")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        text = config_yaml(config)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        raise ConfigError(f"could not write config {target}: {exc}") from exc
    return target


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate a config file, raising :class:`ConfigError`."""

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {target}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config {target}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {target}: {exc}") from exc

    if raw is None:
        raise ConfigError(f"config is empty: {target}")
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a mapping")

    try:
        return AppConfig.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc


def load_or_create_config(
    path: Path | str = DEFAULT_CONFIG_PATH, *, create: bool = True
) -> AppConfig:
    target = Path(path)
    if target.exists():
        return load_config(target)
    if not create:
        raise ConfigError(f"config not found: {target}")
    config = default_config()
    save_config(config, target)
    return config


def merge_config(config: AppConfig, patch: Mapping[str, Any]) -> AppConfig:
    """Deep-merge a patch into a config and re-validate the result."""

    merged = _deep_merge(dump_config(config, reveal_secrets=True), patch)
    try:
        return AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(dict(existing), value)
        else:
            result[key] = value
    return result


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["configuration validation failed:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def validate_interfaces(config: AppConfig, interfaces: Sequence[str]) -> list[str]:
    """Return role assignments that point at unknown interfaces."""

    known = set(interfaces)
    problems: list[str] = []
    for role in ("ap", "airdrop", "wan", "lan"):
        name = getattr(config.roles, role)
        if name in {"auto", "none"}:
            continue
        if name not in known:
            problems.append(f"role {role} points at missing interface {name}")
    return problems

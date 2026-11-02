"""Network module: roles, uplink failover, NAT/nftables and wifi client mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.templates import render_template
from eventhub.core.types import (
    ActionResult,
    HealthResult,
    IssueLevel,
    RenderedFile,
    ValidationIssue,
)
from eventhub.network.hardware import detect_hardware
from eventhub.network.roles import RoleMap, resolve_roles
from eventhub.network.state import (
    NetworkState,
    UplinkState,
    load_state,
    save_state,
)
from eventhub.network.uplink import UplinkManager, WifiNetwork
from eventhub.service_api import ServiceModule


class NetworkService(ServiceModule):
    name = "network"
    title = "Network & Internet"
    description = "Interface roles, uplink selection, failover and nftables NAT."
    config_attr = "network"
    systemd_units = ("eventhub-network.service",)
    packages = ("nftables", "iproute2", "iw")

    @property
    def network(self) -> Any:
        return self.config.network

    def manager(self, config: AppConfig | None = None) -> UplinkManager:
        return UplinkManager(self.runtime, config or self.config)

    def roles(self, config: AppConfig | None = None) -> RoleMap:
        cfg = config or self.config
        try:
            hardware = detect_hardware(self.runtime)
        except Exception:
            hardware = None
        return resolve_roles(cfg, hardware)

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        roles = self.roles(config)
        if config.network.enabled:
            if roles.ap and roles.wan and roles.ap == roles.wan:
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        "the hotspot and WAN must use different interfaces",
                        "roles",
                    )
                )
            if roles.ap and roles.airdrop and roles.ap == roles.airdrop:
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        "the hotspot and AirDrop must use different interfaces",
                        "roles",
                    )
                )
            if config.network.uplink == "wifi" and not roles.wan:
                issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        "wifi uplink selected but no wifi client interface is assigned",
                        "network.wifi_client_interface",
                    )
                )
            if config.network.uplink == "wifi" and not config.network.wifi_networks:
                issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        "wifi uplink selected but no networks are saved; use the dashboard to scan",
                        "network.wifi_networks",
                    )
                )
            if self.runtime.runner.which("nft") is None:
                issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        "nft is not installed; firewall rules will fail until it is",
                        "network.enabled",
                    )
                )
        return issues

    def _wan_interface(self, config: AppConfig | None = None) -> str:
        cfg = config or self.config
        manager = self.manager(cfg)
        roles = manager.roles()
        if cfg.network.uplink == "wifi":
            return manager.wifi_interface() or roles.wan
        if (
            cfg.network.uplink == "auto"
            and cfg.network.wifi_client_interface not in {"auto", ""}
            and not roles.wan
        ):
            return manager.wifi_interface()
        return roles.wan

    def _firewall_context(self, config: AppConfig | None = None) -> dict[str, Any]:
        cfg = config or self.config
        roles = self.roles(cfg)
        wan = self._wan_interface(cfg)
        sharing = bool(
            cfg.network.internet_sharing and cfg.network.uplink != "none" and wan
        )
        return {
            "table": cfg.network.nft_table,
            "ap": roles.ap,
            "lan": roles.lan,
            "wan": wan,
            "sharing": sharing,
            "subnet": cfg.hotspot.subnet,
            "copyparty_port": cfg.fileserver_copyparty.port,
            "dashboard_port": cfg.dashboard.port,
            "smb_enabled": cfg.fileserver_smb.enabled,
            "copyparty_enabled": cfg.fileserver_copyparty.enabled,
            "dashboard_enabled": cfg.dashboard.enabled,
        }

    def render(self, config: AppConfig) -> list[RenderedFile]:
        firewall = render_template(
            "modules/network/nftables.conf.j2", **self._firewall_context(config)
        )
        sysctl = render_template(
            "modules/network/sysctl.conf.j2",
            sharing=self._firewall_context(config)["sharing"],
        )
        return [
            RenderedFile(Path("/etc/eventhub/nftables.conf"), firewall, mode=0o644),
            RenderedFile(
                Path("/etc/sysctl.d/90-eventhub.conf"), sysctl, mode=0o644
            ),
        ]

    def _ensure_rendered(self) -> None:
        for item in self.render(self.config):
            self.runtime.fs.atomic_write(item.path, item.content, mode=item.mode)

    def _apply_rules(self) -> ActionResult:
        if self.runtime.runner.which("nft") is None:
            return ActionResult.failure("nft is not installed")
        self._ensure_rendered()
        if self.runtime.runner.which("sysctl") is not None:
            sysctl = self.runtime.runner.run(["sysctl", "--system"], timeout=30)
            if not sysctl.ok:
                self.log.warning("sysctl --system failed: %s", sysctl.stderr.strip())
        table = self.config.network.nft_table
        self.runtime.runner.run(["nft", "delete", "table", "inet", table], timeout=15)
        result = self.runtime.runner.run(
            ["nft", "-f", "/etc/eventhub/nftables.conf"], timeout=30
        )
        if not result.ok:
            return ActionResult.failure(
                f"nftables load failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        verify = self.runtime.runner.run(
            ["nft", "list", "table", "inet", table], timeout=15
        )
        if not verify.ok:
            return ActionResult.failure("nftables table did not load")
        return ActionResult.success("firewall rules applied")

    def apply(self) -> ActionResult:
        result = self._apply_rules()
        if not result.ok:
            return result
        state = self.uplink_state()
        save_state(
            self.runtime,
            NetworkState(roles=self.roles(), uplink=state),
        )
        self.runtime.bus.emit(
            "network.applied",
            {"roles": self.roles().as_dict(), "uplink": state.kind},
            source="network",
        )
        if self.runtime.systemd.is_active(self.systemd_units[0]):
            self.runtime.systemd.restart(self.systemd_units[0])
        return result

    def start(self) -> ActionResult:
        result = self._apply_rules()
        if not result.ok:
            return result
        return super().start()

    def stop(self) -> ActionResult:
        result = super().stop()
        table = self.config.network.nft_table
        self.runtime.runner.run(["nft", "delete", "table", "inet", table], timeout=15)
        return result

    def health_check(self) -> HealthResult:
        if self.runtime.runner.which("nft") is None:
            return HealthResult(None, "nft is not installed")
        table = self.config.network.nft_table
        result = self.runtime.runner.run(
            ["nft", "list", "table", "inet", table], timeout=10
        )
        if not result.ok:
            return HealthResult(False, f"nftables table {table!r} is missing")
        return HealthResult(True, "firewall loaded", {"table": table})

    def uplink_state(self) -> UplinkState:
        return self.manager().select()

    def saved_state(self) -> NetworkState | None:
        return load_state(self.runtime)

    def test_connectivity(self) -> ActionResult:
        manager = self.manager()
        result = manager.probe()
        if result is True:
            return ActionResult.success("internet reachable")
        if result is False:
            return ActionResult.failure("internet is not reachable")
        return ActionResult.failure("no connectivity probe tool is available (curl missing)")

    def scan_wifi(self, interface: str = "") -> list[WifiNetwork]:
        return self.manager().scan(interface)

    def connect_wifi(self, ssid: str, password: str) -> ActionResult:
        manager = self.manager()
        result = manager.connect(ssid, password)
        if result.ok:
            networks = manager.remember_network(ssid, password)
            config = self.config.model_copy(
                update={
                    "network": self.config.network.model_copy(
                        update={"wifi_networks": networks}
                    )
                }
            )
            self.config = config
            self.runtime.config = config
            self.runtime.bus.emit(
                "network.wifi_connected", {"ssid": ssid}, source="network"
            )
        return result

    def disconnect_wifi(self) -> ActionResult:
        return self.manager().disconnect()

    def wired_clients(self) -> list[dict[str, Any]]:
        roles = self.roles()
        if not roles.lan or self.runtime.runner.which("ip") is None:
            return []
        result = self.runtime.runner.run(
            ["ip", "-j", "neigh", "show", "dev", roles.lan], timeout=10
        )
        if not result.ok or not result.stdout.strip():
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        clients = []
        for item in payload:
            state = str(item.get("state", ""))
            if state in {"REACHABLE", "STALE", "DELAY", "PROBE"}:
                clients.append(
                    {
                        "ip": item.get("dst"),
                        "mac": item.get("lladdr"),
                        "state": state,
                        "interface": roles.lan,
                        "kind": "wired",
                    }
                )
        return clients

    def monitor(self, interval: int | None = None, iterations: int | None = None) -> None:
        """Long-running failover loop used by eventhub-network.service."""

        period = max(5, interval or self.config.network.connectivity_interval)
        last: UplinkState | None = None
        count = 0
        self.log.info("network monitor started (every %ss)", period)
        initial = self._apply_rules()
        if not initial.ok:
            self.log.error("could not apply initial firewall rules: %s", initial.message)
        try:
            while iterations is None or count < iterations:
                state = self.manager().select()
                saved = load_state(self.runtime)
                if last != state:
                    self.log.info(
                        "uplink state: %s %s online=%s (%s)",
                        state.kind,
                        state.interface,
                        state.online,
                        state.detail,
                    )
                    rules = self._apply_rules()
                    if not rules.ok:
                        self.log.error("could not apply firewall rules: %s", rules.message)
                    save_state(
                        self.runtime,
                        NetworkState(roles=self.roles(), uplink=state),
                    )
                    self.runtime.bus.emit(
                        "network.uplink_changed",
                        {"kind": state.kind, "online": state.online},
                        source="network.monitor",
                    )
                    last = state
                elif saved is None:
                    save_state(
                        self.runtime,
                        NetworkState(roles=self.roles(), uplink=state),
                    )
                count += 1
                if iterations is not None and count >= iterations:
                    break
                self.runtime.clock.sleep(period)
        except KeyboardInterrupt:
            self.log.info("network monitor stopped")


SERVICE = NetworkService

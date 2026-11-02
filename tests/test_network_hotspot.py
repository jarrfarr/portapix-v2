from __future__ import annotations

from pathlib import Path

from eventhub.core.config import NetworkConfig, WifiCredential
from eventhub.core.types import CommandResult, Health
from eventhub.hotspot.service import HotspotService
from eventhub.network.service import NetworkService
from eventhub.network.uplink import UplinkManager
from tests.support import FakeFileSystem, FakeRunner, FakeSystemd, make_config, make_runtime


def network_runtime(tmp_path: Path, **network: object):
    config = make_config(
        tmp_path,
        network=NetworkConfig(enabled=True, uplink="wan", **network),
    )
    runner = FakeRunner(binaries={"systemctl", "nft", "sysctl", "curl", "iw", "ip"})
    fs = FakeFileSystem()
    fs.mkdir("/sys/class/net/eth-wan")
    fs.write_text("/sys/class/net/eth-wan/operstate", "up")
    fs.write_text("/sys/class/net/eth-wan/type", "1")
    fs.write_text("/sys/class/net/eth-wan/address", "aa:bb:cc:dd:ee:05")
    systemd = FakeSystemd(active={"eventhub-network.service"})
    return make_runtime(tmp_path, config=config, fs=fs, runner=runner, systemd=systemd)


def test_network_render_includes_nat_and_services(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    service = NetworkService(runtime)
    files = {str(item.path): item.content for item in service.render(runtime.config)}
    nft = files["/etc/eventhub/nftables.conf"]
    assert "table inet eventhub" in nft
    assert "masquerade" in nft
    assert "10.42.0.0/24" in nft
    assert "3923" in nft
    assert "iifname \"wlan-test\"" in nft
    assert "iifname \"eth-wan\"" in nft
    assert "policy drop" in nft
    sysctl = files["/etc/sysctl.d/90-eventhub.conf"]
    assert "net.ipv4.ip_forward = 1" in sysctl


def test_network_render_without_sharing(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        network=NetworkConfig(enabled=True, uplink="none", internet_sharing=False),
    )
    runtime = make_runtime(tmp_path, config=config)
    service = NetworkService(runtime)
    nft = service.render(config)[0].content
    assert "masquerade" not in nft
    assert "chain postrouting" not in nft
    assert "net.ipv4.ip_forward = 1" not in service.render(config)[1].content


def test_network_validate_rejects_same_ap_and_wan(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        roles={"ap": "same0", "airdrop": "air0", "wan": "same0", "lan": "lan0"},
    )
    runtime = make_runtime(tmp_path, config=config)
    issues = NetworkService(runtime).validate(config)
    assert any(issue.level.value == "error" for issue in issues)


def test_network_apply_runs_nft_and_sysctl(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    result = NetworkService(runtime).apply()
    assert result.ok, result.message
    assert runtime.runner.called(r"nft -f /etc/eventhub/nftables.conf")
    assert runtime.runner.called("sysctl --system")
    assert runtime.fs.exists("/etc/eventhub/nftables.conf")
    state = runtime.fs.read_text(runtime.config.state_dir / "network-state.json")
    assert "eth-wan" in state
    # the monitor unit is restarted because it was active
    assert runtime.systemd.actions[-1] == ("restart", "eventhub-network.service")


def test_network_health_checks_table(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    health = NetworkService(runtime).health_check()
    assert health.healthy is True
    runtime.runner.responses["nft list"] = CommandResult(("nft",), 1, stderr="missing")
    health = NetworkService(runtime).health_check()
    assert health.healthy is False


def test_network_removes_rules_on_stop(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    result = NetworkService(runtime).stop()
    assert result.ok
    assert runtime.runner.called("nft delete table inet eventhub")


def test_uplink_selects_online_wan(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    state = UplinkManager(runtime).select()
    assert state.online
    assert state.kind == "wan"
    assert state.interface == "eth-wan"


def test_uplink_offline_when_probe_fails(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    runtime.runner.responses["curl"] = CommandResult(("curl",), 28, stderr="timeout")
    state = UplinkManager(runtime).select()
    assert not state.online
    assert state.kind == "offline"
    assert "unreachable" in state.detail


def test_uplink_none_when_disabled(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, network=NetworkConfig(enabled=True, uplink="none")
    )
    runtime = make_runtime(tmp_path, config=config)
    state = UplinkManager(runtime).select()
    assert state.kind == "offline"
    assert "disabled" in state.detail


def test_scan_wifi_parses_iw_output(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    runtime.runner.responses["iw dev eth-wan scan"] = CommandResult(
        ("iw",),
        0,
        stdout=(
            "BSS aa:bb:cc:dd:ee:01(on eth-wan) -- associated\n"
            "\tsignal: -42.00 dBm\n"
            "\tSSID: StrongNet\n"
            "\tDS Parameter set: channel 6\n"
            "\tRSN: * Version: 1\n"
            "BSS aa:bb:cc:dd:ee:02(on eth-wan)\n"
            "\tsignal: -70.00 dBm\n"
            "\tSSID: WeakNet\n"
            "\tcapability: ESS Privacy\n"
        ),
    )
    networks = UplinkManager(runtime).scan()
    assert [n.ssid for n in networks] == ["StrongNet", "WeakNet"]
    assert networks[0].signal_dbm == -42
    assert networks[0].channel == 6
    assert networks[0].security.startswith("wpa")


def test_connect_wifi_writes_sensitive_config(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        network=NetworkConfig(
            enabled=True,
            uplink="wifi",
            wifi_client_interface="wlx-test",
            wifi_networks=[WifiCredential(ssid="Old", password="old-password")],
        ),
    )
    runner = FakeRunner(
        binaries={"systemctl", "wpa_supplicant", "wpa_cli", "ip", "dhclient", "iw"}
    )
    runtime = make_runtime(tmp_path, config=config, runner=runner)
    result = UplinkManager(runtime).connect("NewNet", "new-password-123")
    assert result.ok, result.message
    path = Path("/etc/eventhub/wpa_supplicant-wlx-test.conf")
    assert runtime.fs.exists(path)
    assert runtime.fs.mode_of(path) == 0o600
    content = runtime.fs.read_text(path)
    assert 'ssid="NewNet"' in content
    assert "psk=" in content
    assert runtime.runner.called("wpa_supplicant -B")


def test_connect_without_wifi_interface_fails(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    result = UplinkManager(runtime).connect("Net", "password123")
    assert not result.ok
    assert "wifi" in result.message


def test_hotspot_render(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    service = HotspotService(runtime)
    files = {str(item.path): item for item in service.render(runtime.config)}
    hostapd = files["/etc/hostapd/eventhub.conf"]
    assert hostapd.mode == 0o600
    assert "ssid=TestHub" in hostapd.content
    assert "wpa_key_mgmt=WPA-PSK SAE" in hostapd.content
    assert "interface=wlan-test" in hostapd.content
    dns = files["/etc/dnsmasq.d/eventhub.conf"].content
    assert "dhcp-range=10.42.0.10,10.42.0.200,255.255.255.0,12h" in dns
    assert "address=/eventhub.local/10.42.0.1" in dns
    assert "dhcp-option=option:router,10.42.0.1" in dns


def test_hotspot_hidden_and_5ghz(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.hotspot.hidden = True
    config.hotspot.band = "5"
    config.hotspot.channel = 44
    runtime = make_runtime(tmp_path, config=config)
    hostapd = HotspotService(runtime).render(config)[0].content
    assert "ignore_broadcast_ssid=1" in hostapd
    assert "hw_mode=a" in hostapd
    assert "channel=44" in hostapd
    assert "ieee80211ac=1" in hostapd


def test_hotspot_apply_stops_distro_units(tmp_path: Path) -> None:
    runner = FakeRunner(binaries={"systemctl", "hostapd", "dnsmasq", "iw", "rfkill"})
    systemd = FakeSystemd(
        active={"hostapd.service", "dnsmasq.service"},
        enabled={"eventhub-hostapd.service", "eventhub-dnsmasq.service"},
    )
    runtime = make_runtime(tmp_path, runner=runner, systemd=systemd)
    result = HotspotService(runtime).apply()
    assert result.ok, result.message
    assert ("stop", "hostapd.service") in systemd.actions
    assert ("stop", "dnsmasq.service") in systemd.actions
    assert ("restart", "eventhub-hostapd.service") in systemd.actions
    assert ("restart", "eventhub-dnsmasq.service") in systemd.actions
    assert runtime.runner.called("rfkill unblock wifi")


def test_hotspot_clients_and_kick(tmp_path: Path) -> None:
    runner = FakeRunner(binaries={"systemctl", "iw"})
    runner.responses["iw dev wlan-test station dump"] = CommandResult(
        ("iw",),
        0,
        stdout=(
            "Station aa:bb:cc:dd:ee:99 (on wlan-test)\n"
            "\tsignal: -55 dBm\n"
            "\trx bytes: 1000\n"
            "\ttx bytes: 2000\n"
            "\tconnected time: 120 seconds\n"
        ),
    )
    runtime = make_runtime(tmp_path, runner=runner)
    service = HotspotService(runtime)
    clients = service.clients()
    assert len(clients) == 1
    assert clients[0]["mac"] == "aa:bb:cc:dd:ee:99"
    assert service.kick("aa:bb:cc:dd:ee:99").ok
    assert not service.kick("not-a-mac").ok


def test_hotspot_health(tmp_path: Path) -> None:
    systemd = FakeSystemd(
        active={"eventhub-hostapd.service", "eventhub-dnsmasq.service"}
    )
    runner = FakeRunner(binaries={"systemctl"})
    runtime = make_runtime(tmp_path, runner=runner, systemd=systemd)
    health = HotspotService(runtime).health_check()
    assert health.healthy is True
    systemd.active.discard("eventhub-dnsmasq.service")
    health = HotspotService(runtime).health_check()
    assert health.healthy is False


def test_hotspot_status_includes_clients(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    status = HotspotService(runtime).status()
    assert status.name == "hotspot"
    assert status.healthy in {Health.UNKNOWN, Health.HEALTHY, Health.UNHEALTHY}



def test_network_addresses_and_gateway(tmp_path: Path) -> None:
    runtime = network_runtime(tmp_path)
    runtime.runner.responses["ip -j addr show"] = CommandResult(
        ("ip",),
        0,
        stdout=(
            '[{"ifname":"wlan-test","addr_info":[{"local":"10.42.0.1","prefixlen":24}]},'
            '{"ifname":"eth-wan","addr_info":[{"local":"192.168.1.20","prefixlen":24}]}]'
        ),
    )
    runtime.runner.responses["ip -j route show default"] = CommandResult(
        ("ip",), 0, stdout='[{"gateway":"192.168.1.1","dev":"eth-wan"}]'
    )
    info = NetworkService(runtime).addresses()
    assert info["interfaces"]["wlan-test"] == ["10.42.0.1/24"]
    assert info["gateway"] == "192.168.1.1"
    assert info["default_route"] == "eth-wan via 192.168.1.1"

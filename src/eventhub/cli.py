"""The single ``eventhub`` command-line entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from eventhub import __version__
from eventhub.core.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    load_config,
    merge_config,
    save_config,
)
from eventhub.core.context import Runtime, build_runtime
from eventhub.core.errors import EventHubError
from eventhub.core.logging import configure_logging
from eventhub.core.types import IssueLevel
from eventhub.registry import ServiceRegistry

console = Console()
error_console = Console(stderr=True)

app = typer.Typer(
    name="eventhub",
    help="Portable Raspberry Pi NAS and transfer hub for event photographers.",
    no_args_is_help=False,
    add_completion=True,
    invoke_without_command=True,
    rich_markup_mode="rich",
)


class CliState:
    def __init__(self, config_path: Path, dry_run: bool, verbose: bool) -> None:
        self.config_path = config_path
        self.dry_run = dry_run
        self.verbose = verbose
        self._runtime: Runtime | None = None

    def runtime(self, *, require_config: bool = True) -> Runtime:
        if self._runtime is not None:
            return self._runtime
        try:
            config = load_config(self.config_path)
        except ConfigError:
            if require_config:
                raise
            from eventhub.core.config import default_config
            from eventhub.core.db import Database

            config = default_config()
            database = Database(":memory:")
            database.migrate()
            runtime = build_runtime(
                config=config,
                config_path=self.config_path,
                db=database,
                dry_run=self.dry_run,
            )
            self._runtime = runtime
            return runtime
        runtime = build_runtime(
            config=config,
            config_path=self.config_path,
            dry_run=self.dry_run,
        )
        self._runtime = runtime
        return runtime

    def runtime_for_install(self) -> Runtime:
        """Runtime for install: use the real DB, or an in-memory DB if non-root."""

        try:
            config = load_config(self.config_path)
        except ConfigError:
            from eventhub.core.config import default_config

            config = default_config()
        from eventhub.core.adapters import is_root
        from eventhub.core.db import Database, open_database

        if is_root():
            database = open_database(config.db_path)
        else:
            database = Database(":memory:")
            database.migrate()
        runtime = build_runtime(
            config=config,
            config_path=self.config_path,
            db=database,
            dry_run=self.dry_run,
        )
        self._runtime = runtime
        return runtime

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"EventHub {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the EventHub configuration file.",
        envvar="EVENTHUB_CONFIG",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render and validate without writing."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """EventHub management.  Run without arguments to open the TUI."""

    configure_logging(level="DEBUG" if verbose else "INFO", console=verbose)
    state = CliState(config_path=config, dry_run=dry_run, verbose=verbose)
    ctx.obj = state
    if ctx.invoked_subcommand is None and not ctx.resilient_parsing:
        code = _launch_tui(state)
        state.close()
        raise typer.Exit(code)


def _state(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if not isinstance(state, CliState):
        raise typer.BadParameter("internal error: CLI state missing")
    return state


def _handle_error(exc: Exception) -> None:
    error_console.print(f"[red]error:[/red] {exc}")
    raise typer.Exit(2)


def _launch_tui(state: CliState) -> int:
    if not sys.stdin.isatty():
        console.print(
            "[yellow]No terminal attached; the TUI needs an interactive stdin.[/yellow]\n"
            "Run [bold]eventhub --help[/bold] for the command-line interface."
        )
        return 2
    try:
        from eventhub.tui.app import run_tui
    except ImportError:
        console.print(
            "[yellow]The terminal UI is not available in this build.[/yellow]\n"
            "Run [bold]eventhub --help[/bold] for the command-line interface."
        )
        return 2
    try:
        config: AppConfig | None
        try:
            config = load_config(state.config_path)
        except ConfigError:
            config = None
        return run_tui(config=config, config_path=state.config_path, dry_run=state.dry_run)
    except EventHubError as exc:
        error_console.print(f"[red]error:[/red] {exc}")
        return 1


# ---------------------------------------------------------------------------
# status / modules
# ---------------------------------------------------------------------------


@app.command()
def status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the state of every module."""

    state = _state(ctx)
    try:
        runtime = state.runtime(require_config=False)
        registry = ServiceRegistry(runtime)
        statuses = registry.statuses()
    except EventHubError as exc:
        _handle_error(exc)
        return
    finally:
        state.close()

    if json_output:
        console.print_json(
            json.dumps({name: s.as_dict() for name, s in statuses.items()}, indent=2)
        )
        return

    table = Table(title="EventHub modules", show_lines=False)
    table.add_column("Module")
    table.add_column("Installed", justify="center")
    table.add_column("Enabled", justify="center")
    table.add_column("Running", justify="center")
    table.add_column("Health")
    table.add_column("Detail", overflow="fold")
    for name, item in statuses.items():
        def marker(value: bool) -> str:
            return "[green]yes[/green]" if value else "[dim]no[/dim]"

        health = {
            "healthy": "[green]healthy[/green]",
            "unhealthy": "[red]unhealthy[/red]",
        }.get(item.healthy.value, "[yellow]unknown[/yellow]")
        table.add_row(
            name,
            marker(item.installed),
            marker(item.enabled),
            marker(item.running),
            health,
            item.detail,
        )
    console.print(table)


@app.command("modules")
def list_modules(ctx: typer.Context) -> None:
    """List available and unavailable modules."""

    state = _state(ctx)
    try:
        runtime = state.runtime(require_config=False)
        infos = ServiceRegistry(runtime).infos()
    except EventHubError as exc:
        _handle_error(exc)
        return
    finally:
        state.close()

    table = Table(title="EventHub module catalogue")
    table.add_column("Name")
    table.add_column("Title")
    table.add_column("Available", justify="center")
    table.add_column("Description", overflow="fold")
    for info in infos:
        table.add_row(
            info.name,
            info.title,
            "[green]yes[/green]" if info.available else "[dim]not in build[/dim]",
            info.description,
        )
    console.print(table)


@app.command("module")
def module_command(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Module name, e.g. hotspot"),
    action: str = typer.Argument(
        ...,
        help="enable, disable, start, stop, restart, status, health, apply or reinstall",
    ),
) -> None:
    """Control one module through the registry."""

    state = _state(ctx)
    try:
        runtime = state.runtime()
        registry = ServiceRegistry(runtime)
        if action in {"enable", "disable"}:
            if not runtime.privileged and not state.dry_run:
                _handle_error(EventHubError("changing module state requires root"))
            config = merge_config(
                runtime.config,
                {_section_for(registry, name): {"enabled": action == "enable"}},
            )
            if not state.dry_run:
                save_config(config, state.config_path)
            runtime = runtime.with_config(config)
            result = ServiceRegistry(runtime).apply(
                names=[name], dry_run=state.dry_run, reload_services=True
            )
            if not result.ok:
                _handle_error(EventHubError(result.message))
            console.print(f"[green]{name} {action}d[/green]")
            return
        if action == "reinstall":
            from eventhub.installer.manager import Installer

            result = Installer(runtime).reinstall_module(name)
        elif action == "receive" and name == "airdrop":
            service = registry.get(name)
            _handle_error_if_missing_airdrop(service)
            service.receive()  # execs into owly receive
            return
        elif action == "doctor" and name == "airdrop":
            service = registry.get(name)
            console.print_json(json.dumps(service.doctor(), indent=2))
            return
        else:
            result = registry.control(name, action)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


def _section_for(registry: ServiceRegistry, name: str) -> str:
    try:
        return registry.get(name).config_key
    except EventHubError:
        return name


def _handle_error_if_missing_airdrop(service: object) -> None:
    if not hasattr(service, "receive"):
        raise EventHubError("this module has no AirDrop receive action")


# ---------------------------------------------------------------------------
# apply / config
# ---------------------------------------------------------------------------


@app.command()
def apply(
    ctx: typer.Context,
    module_names: list[str] = typer.Option(
        [], "--module", "-m", help="Apply only these modules (repeatable)."
    ),
    no_reload: bool = typer.Option(False, "--no-reload", help="Write files without reloading."),
) -> None:
    """Validate, render and atomically apply all configuration."""

    state = _state(ctx)
    try:
        runtime = state.runtime()
        if not runtime.privileged and not state.dry_run:
            _handle_error(EventHubError("apply requires root; use sudo or the helper"))
        registry = ServiceRegistry(runtime)
        result = registry.apply(
            names=module_names or None,
            dry_run=state.dry_run,
            reload_services=not no_reload,
        )
        for issue in result.issues:
            style = {
                IssueLevel.ERROR: "red",
                IssueLevel.WARNING: "yellow",
                IssueLevel.INFO: "dim",
            }[issue.level]
            console.print(f"[{style}]{issue.as_text()}[/{style}]")
        if result.ok:
            console.print(f"[green]{result.message}[/green]")
        else:
            error_console.print(f"[red]{result.message}[/red]")
            raise typer.Exit(1)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


config_app = typer.Typer(help="Inspect and validate the configuration.")
app.add_typer(config_app, name="config")


@config_app.command("path")
def config_path() -> None:
    """Print the configuration file path."""

    console.print(str(DEFAULT_CONFIG_PATH))


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    reveal: bool = typer.Option(
        False, "--reveal", help="Show secrets (requires care; output is not logged)."
    ),
) -> None:
    """Print the effective configuration as YAML."""

    from eventhub.core.config import config_yaml

    state = _state(ctx)
    try:
        config = load_config(state.config_path)
        if reveal:
            console.print(config_yaml(config))
        else:
            import yaml

            console.print(yaml.safe_dump(config.public_dict(), sort_keys=False))
    except EventHubError as exc:
        _handle_error(exc)


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate the configuration without changing anything."""

    state = _state(ctx)
    try:
        runtime = state.runtime()
        issues = ServiceRegistry(runtime).validate_all()
        errors = [issue for issue in issues if issue.level is IssueLevel.ERROR]
        for issue in issues:
            console.print(issue.as_text())
        if errors:
            raise typer.Exit(1)
        console.print("[green]configuration is valid[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@config_app.command("get")
def config_get(ctx: typer.Context, dotted: str) -> None:
    """Read one config value using a dotted key (secrets are masked)."""

    state = _state(ctx)
    try:
        config = load_config(state.config_path)
        value: Any = config.public_dict()
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ConfigError(f"unknown config key: {dotted}")
            value = value[part]
        if isinstance(value, (dict, list)):
            console.print_json(json.dumps(value))
        else:
            console.print(str(value))
    except EventHubError as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# network / hotspot helpers
# ---------------------------------------------------------------------------


network_app = typer.Typer(help="Inspect uplink state, scan wifi and test connectivity.")
app.add_typer(network_app, name="network")

hotspot_app = typer.Typer(help="Inspect the access point and connected clients.")
app.add_typer(hotspot_app, name="hotspot")


@network_app.command("status")
def network_status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show resolved interfaces, uplink state and wired clients."""

    state = _state(ctx)
    try:
        from eventhub.network.service import NetworkService

        runtime = state.runtime()
        service = NetworkService(runtime)
        roles = service.roles()
        uplink = service.uplink_state()
        payload = {
            "roles": roles.as_dict(),
            "uplink": uplink.as_dict(),
            "wired_clients": service.wired_clients(),
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2))
            return
        table = Table(title="Network")
        table.add_column("Item")
        table.add_column("Value")
        for role, value in roles.as_dict().items():
            table.add_row(role, value or "-")
        table.add_row("uplink", f"{uplink.kind} {uplink.interface} online={uplink.online}")
        table.add_row("detail", uplink.detail)
        table.add_row("wired clients", str(len(payload["wired_clients"])))
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@network_app.command("scan")
def network_scan(
    ctx: typer.Context,
    interface: str = typer.Option("", "--interface", "-i"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Scan for wifi networks on the uplink adapter."""

    state = _state(ctx)
    try:
        from eventhub.network.service import NetworkService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("wifi scanning requires root"))
        networks = NetworkService(runtime).scan_wifi(interface)
        if json_output:
            console.print_json(
                json.dumps([network.as_dict() for network in networks], indent=2)
            )
            return
        table = Table(title="Wifi networks")
        table.add_column("SSID")
        table.add_column("Signal", justify="right")
        table.add_column("Security")
        table.add_column("Channel", justify="right")
        for network in networks:
            table.add_row(
                network.ssid,
                f"{network.signal_dbm} dBm",
                network.security,
                str(network.channel or "-"),
            )
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@network_app.command("connect")
def network_connect(
    ctx: typer.Context,
    ssid: str = typer.Argument(...),
    password: str = typer.Option("", "--password", help="Leave empty to prompt."),
) -> None:
    """Connect the wifi client uplink and save the network."""

    state = _state(ctx)
    try:
        from eventhub.core.config import merge_config
        from eventhub.network.service import NetworkService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("connecting wifi requires root"))
        if not password:
            password = typer.prompt("Wifi password", hide_input=True, default="")
        service = NetworkService(runtime)
        result = service.connect_wifi(ssid, password)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        config = merge_config(
            runtime.config,
            {
                "network": {
                    "wifi_networks": [
                        {
                            "ssid": existing.ssid,
                            "password": existing.password.get_secret_value(),
                            "priority": existing.priority,
                            "hidden": existing.hidden,
                        }
                        for existing in runtime.config.network.wifi_networks
                        if existing.ssid != ssid
                    ]
                    + [{"ssid": ssid, "password": password, "priority": 10}]
                }
            },
        )
        save_config(config, state.config_path)
        applied = ServiceRegistry(runtime.with_config(config)).apply(names=["network"])
        if not applied.ok:
            _handle_error(EventHubError(applied.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@network_app.command("test")
def network_test(ctx: typer.Context) -> None:
    """Run a connectivity test against the configured URLs."""

    state = _state(ctx)
    try:
        from eventhub.network.service import NetworkService

        result = NetworkService(state.runtime()).test_connectivity()
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@network_app.command("monitor")
def network_monitor(
    ctx: typer.Context,
    interval: int = typer.Option(0, "--interval", help="Seconds between checks."),
    iterations: int = typer.Option(0, "--iterations", help="Stop after N loops (0 = forever)."),
) -> None:
    """Run the uplink failover monitor (used by systemd)."""

    state = _state(ctx)
    try:
        from eventhub.network.service import NetworkService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("network monitor requires root"))
        NetworkService(runtime).monitor(
            interval=interval or None, iterations=iterations or None
        )
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@network_app.command("stop")
def network_stop(ctx: typer.Context) -> None:
    """Remove EventHub firewall rules."""

    state = _state(ctx)
    try:
        from eventhub.network.service import NetworkService

        result = NetworkService(state.runtime()).stop()
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print("[green]network rules removed[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@hotspot_app.command("status")
def hotspot_status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the AP interface and associated clients."""

    state = _state(ctx)
    try:
        from eventhub.hotspot.service import HotspotService

        service = HotspotService(state.runtime())
        payload = {
            "interface": service.interface(),
            "ssid": service.hotspot.ssid,
            "clients": service.clients(),
            "status": service.status().as_dict(),
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2))
            return
        console.print(
            f"[bold]{service.hotspot.ssid}[/bold] on {service.interface() or '-'} · "
            f"{len(payload['clients'])} client(s)"
        )
        table = Table()
        table.add_column("MAC")
        table.add_column("Signal")
        table.add_column("Connected")
        for client in payload["clients"]:
            table.add_row(
                str(client.get("mac", "")),
                str(client.get("signal", "")),
                str(client.get("connected_time", "")),
            )
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@hotspot_app.command("kick")
def hotspot_kick(ctx: typer.Context, mac: str = typer.Argument(...)) -> None:
    """Disconnect one wifi client by MAC address."""

    state = _state(ctx)
    try:
        from eventhub.hotspot.service import HotspotService

        result = HotspotService(state.runtime()).kick(mac)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# storage / events helpers
# ---------------------------------------------------------------------------


storage_app = typer.Typer(help="Inspect drives, mount, eject and format storage.")
app.add_typer(storage_app, name="storage")

events_app = typer.Typer(help="Create, activate, archive and inspect events.")
app.add_typer(events_app, name="events")


@storage_app.command("status")
def storage_status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the selected drive, mount state and free space."""

    state = _state(ctx)
    try:
        from eventhub.storage.service import StorageService

        service = StorageService(state.runtime())
        payload = {
            "usage": service.usage(),
            "drives": [drive.as_dict() for drive in service.detected_drives()],
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2, default=str))
            return
        usage = payload["usage"]
        table = Table(title="Storage")
        table.add_column("Item")
        table.add_column("Value")
        table.add_row("mount point", str(usage.get("mount_point")))
        table.add_row("mounted", "yes" if usage.get("mounted") else "no")
        if usage.get("total_bytes"):
            table.add_row(
                "used",
                f"{usage['used_bytes'] / 1024**3:.1f} / "
                f"{usage['total_bytes'] / 1024**3:.1f} GiB "
                f"({usage['used_percent']}%)",
            )
        table.add_row("required", "yes" if usage.get("required") else "no")
        console.print(table)
        drives = Table(title="Detected drives")
        drives.add_column("Device")
        drives.add_column("UUID")
        drives.add_column("Label")
        drives.add_column("FS")
        drives.add_column("Size", justify="right")
        drives.add_column("Mounted")
        for drive in payload["drives"]:
            drives.add_row(
                drive["device"],
                drive["uuid"],
                drive["label"],
                drive["fstype"],
                f"{drive['size_bytes'] / 1024**3:.1f} GiB",
                drive["mount_point"] or "-",
            )
        console.print(drives)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@storage_app.command("mount")
def storage_mount(ctx: typer.Context) -> None:
    """Mount the configured storage drive."""

    state = _state(ctx)
    try:
        from eventhub.storage.service import StorageService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("mounting requires root"))
        result = StorageService(runtime).mount()
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@storage_app.command("unmount")
def storage_unmount(
    ctx: typer.Context, force: bool = typer.Option(False, "--force")
) -> None:
    """Safely eject the storage drive."""

    state = _state(ctx)
    try:
        from eventhub.storage.service import StorageService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("unmounting requires root"))
        result = StorageService(runtime).unmount(force=force)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@storage_app.command("format")
def storage_format(
    ctx: typer.Context,
    fstype: str = typer.Option("ext4", "--fstype", help="ext4 or exfat"),
    confirm: str = typer.Option("", "--confirm", help="Type FORMAT to confirm."),
    label: str = typer.Option("EVENTHUB", "--label"),
) -> None:
    """Format the selected removable drive. Destructive; requires FORMAT."""

    state = _state(ctx)
    try:
        from eventhub.storage.service import StorageService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("formatting requires root"))
        result = StorageService(runtime).format(
            fstype=fstype, confirmation=confirm, label=label
        )
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


def _persist_events_config(state: CliState, runtime: Runtime) -> None:
    from eventhub.core.config import config_yaml

    runtime.fs.atomic_write(state.config_path, config_yaml(runtime.config), mode=0o600)


@events_app.command("list")
def events_list(
    ctx: typer.Context,
    all_events: bool = typer.Option(False, "--all", help="Include archived events."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List events and the active event."""

    state = _state(ctx)
    try:
        from eventhub.events.service import EventsService

        service = EventsService(state.runtime())
        events = service.list_events(include_archived=all_events)
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "active": state.runtime.config.events.active_event,
                        "events": [event.as_dict() for event in events],
                    },
                    indent=2,
                )
            )
            return
        table = Table(title="Events")
        table.add_column("Slug")
        table.add_column("Name")
        table.add_column("Date")
        table.add_column("Status")
        table.add_column("Size", justify="right")
        for event in events:
            marker = "*" if state.runtime.config.events.active_event == event.slug else ""
            table.add_row(
                event.slug + marker,
                event.name,
                event.event_date,
                event.status,
                f"{service.size_bytes(event.slug) / 1024**2:.1f} MiB",
            )
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@events_app.command("create")
def events_create(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    event_date: str = typer.Option("", "--date", help="ISO date (default: today)."),
    slug: str = typer.Option("", "--slug"),
    notes: str = typer.Option("", "--notes"),
    activate: bool = typer.Option(False, "--activate", help="Activate the new event."),
    no_apply: bool = typer.Option(False, "--no-apply"),
) -> None:
    """Create an event with its folder layout."""

    state = _state(ctx)
    try:
        from eventhub.events.service import EventsService

        runtime = state.runtime()
        if not runtime.privileged and not state.dry_run:
            _handle_error(EventHubError("creating events requires root"))
        service = EventsService(runtime)
        event = service.create_event(
            name,
            event_date=event_date or None,
            slug=slug or None,
            notes=notes,
            activate=True if activate else None,
        )
        _persist_events_config(state, runtime)
        if not no_apply:
            applied = ServiceRegistry(runtime).apply()
            if not applied.ok:
                _handle_error(EventHubError(applied.message))
        console.print(
            f"[green]created {event.slug} at {event.path}[/green]"
            + (" (active)" if runtime.config.events.active_event == event.slug else "")
        )
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@events_app.command("activate")
def events_activate(ctx: typer.Context, slug: str = typer.Argument(...)) -> None:
    """Make an event the AirDrop/default upload target."""

    state = _state(ctx)
    try:
        from eventhub.events.service import EventsService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("activating events requires root"))
        EventsService(runtime).activate(slug)
        _persist_events_config(state, runtime)
        applied = ServiceRegistry(runtime).apply()
        if not applied.ok:
            _handle_error(EventHubError(applied.message))
        console.print(f"[green]{slug} is now active[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@events_app.command("archive")
def events_archive(ctx: typer.Context, slug: str = typer.Argument(...)) -> None:
    """Archive an event (read-only)."""

    state = _state(ctx)
    try:
        from eventhub.events.service import EventsService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("archiving events requires root"))
        EventsService(runtime).archive(slug)
        _persist_events_config(state, runtime)
        applied = ServiceRegistry(runtime).apply()
        if not applied.ok:
            _handle_error(EventHubError(applied.message))
        console.print(f"[green]{slug} archived[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@events_app.command("restore")
def events_restore(ctx: typer.Context, slug: str = typer.Argument(...)) -> None:
    """Re-open an archived event."""

    state = _state(ctx)
    try:
        from eventhub.events.service import EventsService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("restoring events requires root"))
        EventsService(runtime).restore(slug)
        console.print(f"[green]{slug} restored[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@events_app.command("path")
def events_path(ctx: typer.Context, slug: str = typer.Argument("")) -> None:
    """Print the active (or named) event's folder."""

    state = _state(ctx)
    try:
        from eventhub.events.service import EventsService

        service = EventsService(state.runtime())
        event = service.get_event(slug) if slug else service.active_event()
        if event is None:
            _handle_error(EventHubError("no active event"))
            return
        console.print(str(event.path))
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# users / fileservers
# ---------------------------------------------------------------------------


users_app = typer.Typer(help="Create users, guests and groups; manage roles.")
app.add_typer(users_app, name="users")

smb_app = typer.Typer(help="Inspect SMB shares and account sync.")
app.add_typer(smb_app, name="smb")

copyparty_app = typer.Typer(help="Inspect copyparty volumes and status.")
app.add_typer(copyparty_app, name="copyparty")


def _resolve_user(service: Any, identifier: str) -> Any:
    if identifier.isdigit():
        return service.get_user(int(identifier))
    user = service.get_by_username(identifier)
    if user is None:
        raise EventHubError(f"user not found: {identifier}")
    return user


def _apply_fileservers(runtime: Runtime) -> None:
    names = [
        name
        for name in ("fileserver_copyparty", "fileserver_smb")
        if ServiceRegistry(runtime).is_available(name)
    ]
    if names:
        result = ServiceRegistry(runtime).apply(names=names)
        if not result.ok:
            raise EventHubError(result.message)


@users_app.command("list")
def users_list(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """List users, roles and expiry."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        users = UsersService(state.runtime()).list_users()
        if json_output:
            console.print_json(json.dumps([user.as_dict() for user in users], indent=2))
            return
        table = Table(title="Users")
        table.add_column("Username")
        table.add_column("Role")
        table.add_column("Enabled")
        table.add_column("Guest")
        table.add_column("Expires")
        table.add_column("Last login")
        for user in users:
            table.add_row(
                user.username,
                user.role,
                "yes" if user.enabled else "no",
                "yes" if user.is_guest else "no",
                user.expires_at or "-",
                user.last_login_at or "-",
            )
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("create")
def users_create(
    ctx: typer.Context,
    username: str = typer.Argument(...),
    role: str = typer.Option("viewer", "--role"),
    password: str = typer.Option("", "--password", help="Leave empty to prompt."),
    display_name: str = typer.Option("", "--display-name"),
    no_change: bool = typer.Option(False, "--no-change", help="Do not force a password change."),
) -> None:
    """Create a user and sync SMB/copyparty credentials."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("creating users requires root"))
        if not password:
            password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        service = UsersService(runtime)
        user = service.create_user(
            username,
            password,
            role=role,
            display_name=display_name,
            must_change_password=not no_change,
        )
        _apply_fileservers(runtime)
        console.print(f"[green]created user {user.username} ({user.role})[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("passwd")
def users_passwd(
    ctx: typer.Context,
    username: str = typer.Argument(...),
    password: str = typer.Option("", "--password"),
    generate: bool = typer.Option(False, "--generate"),
) -> None:
    """Set a user's password and sync SMB."""

    state = _state(ctx)
    try:
        from eventhub.core.config import generate_wifi_password
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("changing passwords requires root"))
        service = UsersService(runtime)
        user = _resolve_user(service, username)
        if generate:
            password = generate_wifi_password()
        if not password:
            password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
        result = service.set_password(user.id, password)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        _apply_fileservers(runtime)
        console.print(f"[green]password updated for {user.username}[/green]")
        if generate:
            console.print(f"Password: [bold]{password}[/bold]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("delete")
def users_delete(ctx: typer.Context, username: str = typer.Argument(...)) -> None:
    """Delete a user, their sessions and protocol credentials."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("deleting users requires root"))
        service = UsersService(runtime)
        user = _resolve_user(service, username)
        result = service.delete_user(user.id)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        _apply_fileservers(runtime)
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("role")
def users_role(
    ctx: typer.Context,
    username: str = typer.Argument(...),
    role: str = typer.Argument(...),
) -> None:
    """Change a user's role (admin, editor, shooter, viewer)."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        service = UsersService(runtime)
        user = _resolve_user(service, username)
        service.update_user(user.id, role=role)
        _apply_fileservers(runtime)
        console.print(f"[green]{user.username} is now {role}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("guest")
def users_guest(
    ctx: typer.Context,
    hours: int = typer.Option(24, "--hours"),
    role: str = typer.Option("shooter", "--role"),
) -> None:
    """Create a temporary guest account and print its one-time password."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("creating guests requires root"))
        user, password = UsersService(runtime).create_guest_account(
            hours=hours, role=role
        )
        _apply_fileservers(runtime)
        console.print(
            f"[green]guest {user.username} created[/green]\n"
            f"Password: [bold]{password}[/bold]\nExpires: {user.expires_at}"
        )
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("link")
def users_link(
    ctx: typer.Context,
    event_id: int = typer.Option(0, "--event-id", help="Restrict to this event id."),
    permission: str = typer.Option("upload", "--permission"),
    hours: int = typer.Option(24, "--hours"),
    max_uses: int = typer.Option(0, "--max-uses"),
    label: str = typer.Option("", "--label"),
) -> None:
    """Create an expiring guest upload/read link (token shown once)."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("creating links requires root"))
        link = UsersService(runtime).create_guest_link(
            event_id=event_id or None,
            permission=permission,
            hours=hours,
            max_uses=max_uses or None,
            label=label,
        )
        console.print(
            f"[green]link token:[/green] [bold]{link.token}[/bold]\n"
            f"Expires: {link.expires_at}"
        )
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("sync")
def users_sync(ctx: typer.Context) -> None:
    """Ensure SMB system users exist and re-render file server config."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("syncing users requires root"))
        result = UsersService(runtime).sync_backends()
        _apply_fileservers(runtime)
        console.print(json.dumps(result, indent=2))
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@users_app.command("groups")
def users_groups(ctx: typer.Context) -> None:
    """List groups and their members."""

    state = _state(ctx)
    try:
        from eventhub.users.service import UsersService

        groups = UsersService(state.runtime()).list_groups()
        table = Table(title="Groups")
        table.add_column("Name")
        table.add_column("Members")
        for group in groups:
            table.add_row(group.name, ", ".join(group.members))
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@smb_app.command("status")
def smb_status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show SMB shares and health."""

    state = _state(ctx)
    try:
        from eventhub.fileserver_smb.service import SmbService

        service = SmbService(state.runtime())
        payload = {
            "shares": service.shares_list(),
            "status": service.status().as_dict(),
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2))
            return
        table = Table(title="SMB shares")
        table.add_column("Name")
        table.add_column("Path")
        table.add_column("Readers")
        table.add_column("Writers")
        for share in payload["shares"]:
            table.add_row(
                share["name"],
                share["path"],
                ", ".join(share["valid_users"]),
                ", ".join(share["write_users"]) or "-",
            )
        console.print(table)
        console.print(f"health: {service.health_check().message}")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@smb_app.command("sync")
def smb_sync(ctx: typer.Context) -> None:
    """Ensure every enabled user has an SMB system account (passwords unchanged)."""

    state = _state(ctx)
    try:
        from eventhub.fileserver_smb.service import SmbService
        from eventhub.users.service import UsersService

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("syncing SMB users requires root"))
        result = SmbService(runtime).ensure_users(UsersService(runtime).list_users())
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@copyparty_app.command("status")
def copyparty_status(
    ctx: typer.Context, json_output: bool = typer.Option(False, "--json")
) -> None:
    """Show copyparty health and volumes."""

    state = _state(ctx)
    try:
        from eventhub.fileserver_copyparty.service import CopypartyService

        service = CopypartyService(state.runtime())
        payload = {
            "volumes": service.volumes_list(),
            "status": service.status().as_dict(),
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2))
            return
        console.print(f"health: {service.health_check().message}")
        table = Table(title="copyparty volumes")
        table.add_column("Event")
        table.add_column("Path")
        table.add_column("Archived")
        table.add_column("Access")
        for volume in payload["volumes"]:
            table.add_row(
                volume["slug"],
                volume["path"],
                "yes" if volume["archived"] else "no",
                json.dumps(volume["accs"]),
            )
        console.print(table)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# airdrop / discovery helpers
# ---------------------------------------------------------------------------


airdrop_app = typer.Typer(help="Inspect AirDrop/owly status and the target folder.")
app.add_typer(airdrop_app, name="airdrop")

discovery_app = typer.Typer(help="Inspect mDNS advertisement.")
app.add_typer(discovery_app, name="discovery")


@airdrop_app.command("status")
def airdrop_status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show owly units, AWDL interface and receive target."""

    state = _state(ctx)
    try:
        from eventhub.airdrop.service import AirdropService

        service = AirdropService(state.runtime())
        payload = {
            "interface": service.interface(),
            "awdl_interface": service.airdrop.awdl_interface,
            "target": str(service.target_dir()),
            "health": service.health_check().message,
            "argv": service.receive_argv(),
            "status": service.status().as_dict(),
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2))
            return
        for key in ("interface", "awdl_interface", "target", "health"):
            console.print(f"{key}: {payload[key]}")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@airdrop_app.command("doctor")
def airdrop_doctor(ctx: typer.Context) -> None:
    """Run `owly doctor --json` and print the report."""

    state = _state(ctx)
    try:
        from eventhub.airdrop.service import AirdropService

        console.print_json(json.dumps(AirdropService(state.runtime()).doctor(), indent=2))
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@airdrop_app.command("target")
def airdrop_target(ctx: typer.Context) -> None:
    """Print the folder received files will land in."""

    state = _state(ctx)
    try:
        from eventhub.airdrop.service import AirdropService

        console.print(str(AirdropService(state.runtime()).target_dir()))
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@discovery_app.command("status")
def discovery_status(
    ctx: typer.Context, json_output: bool = typer.Option(False, "--json")
) -> None:
    """Show the advertised mDNS name and services."""

    state = _state(ctx)
    try:
        from eventhub.discovery.service import DiscoveryService

        service = DiscoveryService(state.runtime())
        config = state.runtime.config
        payload = {
            "fqdn": f"{config.discovery.hostname}.{config.discovery.domain}",
            "interfaces": service.interfaces(),
            "smb": config.discovery.advertise_smb,
            "copyparty": config.discovery.advertise_copyparty,
            "dashboard": config.discovery.advertise_dashboard,
            "health": service.health_check().message,
            "status": service.status().as_dict(),
        }
        if json_output:
            console.print_json(json.dumps(payload, indent=2))
            return
        for key, value in payload.items():
            if key not in {"status"}:
                console.print(f"{key}: {value}")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# helper socket
# ---------------------------------------------------------------------------


helper_app = typer.Typer(help="Run or query the privileged helper socket.")
app.add_typer(helper_app, name="helper")


@helper_app.command("serve")
def helper_serve(ctx: typer.Context) -> None:
    """Run the root helper daemon (normally started by systemd)."""

    state = _state(ctx)
    try:
        from eventhub.helper.server import run_helper

        runtime = state.runtime()
        if not runtime.privileged:
            _handle_error(EventHubError("the helper must run as root"))
        run_helper(runtime)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@helper_app.command("ping")
def helper_ping(ctx: typer.Context) -> None:
    """Check whether the helper socket is reachable."""

    state = _state(ctx)
    try:
        from eventhub.helper.client import HelperClient

        client = HelperClient(state.runtime().config.helper.socket_path)
        if not client.ping():
            _handle_error(EventHubError("helper socket is not reachable"))
        console.print("[green]helper socket is reachable[/green]")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@helper_app.command("call")
def helper_call(
    ctx: typer.Context,
    action: str = typer.Argument(...),
    payload: str = typer.Option("{}", "--payload", help="JSON object payload."),
) -> None:
    """Call one allow-listed helper action and print the JSON response."""

    state = _state(ctx)
    try:
        import json as json_module

        from eventhub.helper.client import HelperClient

        try:
            decoded = json_module.loads(payload)
        except ValueError:
            _handle_error(EventHubError("payload is not valid JSON"))
            return
        if not isinstance(decoded, dict):
            _handle_error(EventHubError("payload must be a JSON object"))
            return
        response = HelperClient(
            state.runtime().config.helper.socket_path
        ).call_sync(action, decoded)
        console.print_json(json_module.dumps(response.as_dict(), indent=2))
        if not response.ok:
            raise typer.Exit(1)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# placeholders wired up in later phases
# ---------------------------------------------------------------------------


@app.command()
def install(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Unattended install."),
    answers: Path | None = typer.Option(
        None, "--answers", "--config", help="Unattended answers file (YAML)."
    ),
    skip_packages: bool = typer.Option(
        False, "--skip-packages", help="Skip system package installation (development)."
    ),
    no_start: bool = typer.Option(False, "--no-start", help="Do not start services."),
    selftest_after: bool = typer.Option(
        False, "--selftest", help="Run the self-test after installing."
    ),
) -> None:
    """Install or repair EventHub (idempotent)."""

    state = _state(ctx)
    try:
        from eventhub.installer.manager import Installer

        runtime = state.runtime_for_install()
        if not runtime.privileged and not state.dry_run:
            _handle_error(EventHubError("install requires root"))
        result = Installer(runtime).install(
            unattended=yes,
            answers_path=answers,
            dry_run=state.dry_run,
            skip_packages=skip_packages,
            start_services=not no_start,
            run_selftest=selftest_after,
        )
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(f"[green]{result.message}[/green]")
        if result.admin_password:
            console.print(
                f"[yellow]Initial admin password: {result.admin_password}[/yellow] "
                "(also written to the first-boot password file)"
            )
        for warning in result.warnings:
            console.print(f"[yellow]warning:[/yellow] {warning}")
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@app.command()
def update(
    ctx: typer.Context,
    check: bool = typer.Option(False, "--check", help="Only check for an update."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Update EventHub from GitHub, with config backup and rollback."""

    state = _state(ctx)
    try:
        from eventhub.installer.manager import Installer

        runtime = state.runtime()
        result = Installer(runtime).update(check_only=check, yes=yes)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(result.message)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@app.command()
def uninstall(
    ctx: typer.Context,
    mode: str = typer.Option(
        "services-only",
        "--mode",
        help="services-only, keep-data, or full-wipe",
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    confirm: str = typer.Option("", "--confirm", help="Typed confirmation for destructive modes."),
) -> None:
    """Remove EventHub services, configuration or everything."""

    state = _state(ctx)
    try:
        from eventhub.installer.manager import Installer

        runtime = state.runtime(require_config=False)
        if not runtime.privileged:
            _handle_error(EventHubError("uninstall requires root"))
        result = Installer(runtime).uninstall(mode=mode, yes=yes, confirm=confirm)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(result.message)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@app.command()
def recovery(
    ctx: typer.Context,
    action: str = typer.Option(
        "status",
        "--action",
        help="status, reset-admin, reset-hotspot, detect-hardware",
    ),
) -> None:
    """Recovery actions for a broken or locked-out hub."""

    state = _state(ctx)
    try:
        from eventhub.system.recovery import RecoveryManager

        runtime = state.runtime()
        manager = RecoveryManager(runtime)
        result = manager.run(action)
        if not result.ok:
            _handle_error(EventHubError(result.message))
        console.print(result.message)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@app.command()
def selftest(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run live checks for every service, port, mount and route."""

    state = _state(ctx)
    try:
        from eventhub.system.selftest import SelfTest

        runtime = state.runtime()
        report = SelfTest(runtime).run()
        if json_output:
            console.print_json(json.dumps(report.as_dict(), indent=2))
        else:
            report.print(console)
        if not report.ok:
            raise typer.Exit(1)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


@app.command("serve")
def serve(
    ctx: typer.Context,
    host: str = typer.Option("", help="Override bind address."),
    port: int = typer.Option(0, help="Override bind port."),
) -> None:
    """Run the dashboard (normally started by systemd)."""

    state = _state(ctx)
    try:
        from eventhub.dashboard.app import run_dashboard

        runtime = state.runtime()
        run_dashboard(runtime, host=host or None, port=port or None)
    except EventHubError as exc:
        _handle_error(exc)
    finally:
        state.close()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        error_console.print("[yellow]interrupted[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()

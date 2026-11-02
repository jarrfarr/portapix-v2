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

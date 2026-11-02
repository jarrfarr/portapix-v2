"""Build/install owly (omdrop-owl) from source when a prebuilt package is absent."""

from __future__ import annotations

import re
from dataclasses import dataclass

from eventhub.core.config import AirdropConfig, merge_config
from eventhub.core.context import Runtime
from eventhub.core.logging import log_marker


@dataclass(frozen=True, slots=True)
class OwlyBuildResult:
    ok: bool
    message: str
    version: str = ""
    disabled: bool = False


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return (0,)
    return tuple(int(part or 0) for part in match.groups())


def install_owly(runtime: Runtime, config: AirdropConfig) -> OwlyBuildResult:
    log = runtime.logger.getChild("airdrop.build")
    if runtime.fs.exists(config.owl_binary) and runtime.fs.exists(config.owl_daemon):
        return OwlyBuildResult(True, f"owly already installed at {config.owl_binary}")
    if runtime.runner.which("owly") is not None and runtime.runner.which("owlyd") is not None:
        return OwlyBuildResult(True, "owly already installed")
    if not config.build_on_install:
        return OwlyBuildResult(
            False,
            "owly is not installed and build_on_install is disabled",
            disabled=True,
        )
    cargo = runtime.runner.which("cargo")
    if cargo is None:
        return OwlyBuildResult(
            False,
            "Rust/Cargo is missing. Install Rust 1.88+ (https://rustup.rs), then "
            "re-run the installer; AirDrop has been disabled for now.",
            disabled=True,
        )
    version_result = runtime.runner.run([cargo, "--version"], timeout=30)
    version = version_result.stdout.strip()
    if not version_result.ok or _version_tuple(version) < _version_tuple(config.owl_min_rust):
        return OwlyBuildResult(
            False,
            f"cargo {version or 'unknown'} is older than {config.owl_min_rust}; "
            "install a newer Rust toolchain. AirDrop has been disabled for now.",
            version=version,
            disabled=True,
        )
    source = config.owl_dir
    try:
        if runtime.fs.exists(source / ".git"):
            fetch = runtime.runner.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    config.owl_ref,
                ],
                timeout=300,
            )
            if not fetch.ok:
                raise RuntimeError(fetch.stderr.strip() or "git fetch failed")
            checkout = runtime.runner.run(
                ["git", "-C", str(source), "checkout", "-q", "FETCH_HEAD"], timeout=60
            )
            if not checkout.ok:
                raise RuntimeError(checkout.stderr.strip() or "git checkout failed")
        else:
            runtime.fs.mkdir(source.parent, mode=0o755)
            clone = runtime.runner.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    config.owl_ref,
                    config.owl_repo,
                    str(source),
                ],
                timeout=900,
            )
            if not clone.ok:
                raise RuntimeError(clone.stderr.strip() or "git clone failed")
        log_marker(log, "info", "building owly (this can take 10-20 minutes on a Pi)")
        build = runtime.runner.run(
            [cargo, "build", "--release", "--locked", "-p", "owlyd", "-p", "owly-cli"],
            timeout=3600,
            cwd=source,
        )
        if not build.ok:
            raise RuntimeError(build.stderr[-2000:] or "cargo build failed")
        install = runtime.runner.run(
            ["sh", "packaging/install.sh", "owly"], timeout=300, cwd=source
        )
        if not install.ok:
            raise RuntimeError(install.stderr[-1000:] or "packaging/install.sh failed")
        runtime.systemd.daemon_reload()
    except (OSError, RuntimeError) as exc:
        return OwlyBuildResult(
            False,
            f"could not build owly: {exc}. AirDrop has been disabled for now.",
            disabled=True,
        )
    return OwlyBuildResult(True, "owly built and installed", version=version)


def disable_airdrop(config: object, reason: str) -> object:
    from eventhub.core.config import AppConfig

    assert isinstance(config, AppConfig)
    return merge_config(config, {"airdrop": {"enabled": False}})

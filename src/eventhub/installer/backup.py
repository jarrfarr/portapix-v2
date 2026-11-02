"""Config/user-database backup and restore."""

from __future__ import annotations

import contextlib
import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from eventhub import __version__
from eventhub.core.config import AppConfig, ConfigError, config_yaml
from eventhub.core.context import Runtime
from eventhub.core.db import utcnow


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: str
    size_bytes: int
    version: str
    includes_events: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "version": self.version,
            "includes_events": self.includes_events,
        }


class BackupManager:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.directory = runtime.config.state_dir / "backups"

    def _safe_member(self, name: str) -> bool:
        path = Path(name)
        return not path.is_absolute() and ".." not in path.parts

    def export(self, *, include_events: bool = True) -> BackupInfo:
        self.runtime.fs.mkdir(self.directory, mode=0o700)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = self.directory / f"eventhub-backup-{stamp}.zip"
        events_metadata: list[dict[str, Any]] = []
        if include_events:
            try:
                rows = self.runtime.db.query_all("SELECT * FROM events ORDER BY id")
                events_metadata = [dict(row) for row in rows]
            except Exception:
                events_metadata = []
        manifest = {
            "format": 1,
            "version": __version__,
            "created_at": utcnow(),
            "includes_events": include_events,
            "config_path": str(self.runtime.config_path),
        }
        with tempfile.TemporaryDirectory(prefix="eventhub-backup-") as temp_dir:
            db_copy = Path(temp_dir) / "eventhub.db"
            self.runtime.db.backup(db_copy)
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
                archive.writestr("config.yaml", config_yaml(self.runtime.config))
                archive.write(db_copy, "eventhub.db")
                if include_events:
                    archive.writestr(
                        "events.json", json.dumps(events_metadata, indent=2, default=str) + "\n"
                    )
        with contextlib.suppress(OSError):
            self.runtime.fs.chmod(target, 0o600)
        info = BackupInfo(
            path=target,
            created_at=str(manifest["created_at"]),
            size_bytes=target.stat().st_size,
            version=__version__,
            includes_events=include_events,
        )
        self.runtime.db.audit("backup.export", target=str(target), detail=info.created_at)
        return info

    def list_backups(self) -> list[BackupInfo]:
        if not self.directory.is_dir():
            return []
        result: list[BackupInfo] = []
        for path in sorted(self.directory.glob("eventhub-backup-*.zip"), reverse=True):
            try:
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                result.append(
                    BackupInfo(
                        path=path,
                        created_at=str(manifest.get("created_at", "")),
                        size_bytes=path.stat().st_size,
                        version=str(manifest.get("version", "")),
                        includes_events=bool(manifest.get("includes_events", False)),
                    )
                )
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                continue
        return result

    def restore(self, path: Path | str, *, actor: str = "system") -> None:
        source = Path(path)
        if not source.is_file():
            raise ConfigError(f"backup not found: {source}")
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                for name in names:
                    if not self._safe_member(name):
                        raise ConfigError(f"unsafe path in backup: {name}")
                try:
                    manifest = json.loads(archive.read("manifest.json"))
                    config_text = archive.read("config.yaml").decode("utf-8")
                    db_bytes = archive.read("eventhub.db")
                except KeyError as exc:
                    raise ConfigError(f"backup is missing {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise ConfigError("backup is not a valid zip archive") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise ConfigError("unsupported backup format")
        try:
            raw = yaml.safe_load(config_text)
            config = AppConfig.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"backup config is invalid: {exc}") from exc
        with tempfile.TemporaryDirectory(prefix="eventhub-restore-") as temp_dir:
            db_file = Path(temp_dir) / "eventhub.db"
            db_file.write_bytes(db_bytes)
            self.runtime.db.restore_from(db_file)
        self.runtime.fs.atomic_write(self.runtime.config_path, config_yaml(config), mode=0o600)
        self.runtime.config = config
        self.runtime.db.audit(
            "backup.restore",
            actor=actor,
            target=str(source),
            detail=str(manifest.get("created_at", "")),
        )

    def delete(self, path: Path | str) -> None:
        target = Path(path)
        if target.parent != self.directory:
            raise ConfigError("refusing to delete a file outside the backup directory")
        self.runtime.fs.remove(target, missing_ok=True)

"""Atomic apply engine with snapshots and rollback.

Rendered files are written to temporary files and ``os.replace``d into place
only after every file has been validated and rendered.  A failed service
reload restores the previous files and reloads the previous configuration.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from eventhub.core.context import Runtime
from eventhub.core.errors import EventHubError
from eventhub.core.types import (
    ActionResult,
    ApplyResult,
    IssueLevel,
    RenderedFile,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule

_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    root: Path
    manifest: Path
    created_at: str
    files: tuple[Path, ...]


class ApplyEngine:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.snapshots_dir = runtime.config.state_dir / "snapshots"
        self.log = runtime.logger.getChild("apply")

    # -- validation ----------------------------------------------------
    def validate(self, services: list[ServiceModule]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        by_name = {service.name: service for service in services}
        for service in services:
            try:
                section_issues = service.validate(self.runtime.config)
            except Exception as exc:  # a broken validator must not crash apply
                section_issues = [
                    ValidationIssue(
                        level=IssueLevel.ERROR,
                        message=f"validator crashed: {exc}",
                        field=service.config_key,
                    )
                ]
            for issue in section_issues:
                if not service.enabled and issue.level is IssueLevel.ERROR:
                    issues.append(
                        ValidationIssue(
                            level=IssueLevel.WARNING,
                            message=f"{service.name} is disabled; {issue.message}",
                            field=issue.field,
                        )
                    )
                else:
                    issues.append(issue)
            if service.enabled:
                for dependency in service.depends_on:
                    dep = by_name.get(dependency)
                    if dep is not None and not dep.enabled:
                        issues.append(
                            ValidationIssue(
                                level=IssueLevel.WARNING,
                                message=(
                                    f"{service.name} is enabled but depends on disabled "
                                    f"module {dependency}"
                                ),
                                field=service.config_key,
                            )
                        )
        return issues

    def render(
        self, services: list[ServiceModule]
    ) -> tuple[dict[str, list[RenderedFile]], list[ValidationIssue]]:
        rendered: dict[str, list[RenderedFile]] = {}
        issues: list[ValidationIssue] = []
        for service in services:
            if not service.enabled:
                continue
            try:
                files = service.render(self.runtime.config)
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        level=IssueLevel.ERROR,
                        message=f"{service.name} render failed: {exc}",
                        field=service.config_key,
                    )
                )
                continue
            checked: list[RenderedFile] = []
            for item in files:
                path = Path(item.path)
                if not path.is_absolute():
                    issues.append(
                        ValidationIssue(
                            level=IssueLevel.ERROR,
                            message=f"{service.name} rendered a relative path: {path}",
                            field=service.config_key,
                        )
                    )
                    continue
                if ".." in path.parts:
                    issues.append(
                        ValidationIssue(
                            level=IssueLevel.ERROR,
                            message=f"{service.name} rendered an unsafe path: {path}",
                            field=service.config_key,
                        )
                    )
                    continue
                checked.append(item)
            rendered[service.name] = checked
        return rendered, issues

    # -- apply ---------------------------------------------------------
    def apply(
        self,
        services: list[ServiceModule],
        *,
        dry_run: bool = False,
        reload_services: bool = True,
    ) -> ApplyResult:
        issues = self.validate(services)
        errors = [issue for issue in issues if issue.level is IssueLevel.ERROR]
        if errors:
            return ApplyResult(
                ok=False,
                message="validation failed; nothing was changed",
                issues=tuple(issues),
            )

        rendered, render_issues = self.render(services)
        issues.extend(render_issues)
        if any(issue.level is IssueLevel.ERROR for issue in render_issues):
            return ApplyResult(
                ok=False,
                message="render failed; nothing was changed",
                issues=tuple(issues),
            )

        enabled = [s for s in services if s.enabled]
        if dry_run:
            files = sum(len(items) for items in rendered.values())
            return ApplyResult(
                ok=True,
                message=f"dry run: would apply {len(enabled)} modules and {files} files",
                applied=tuple(s.name for s in enabled),
                issues=tuple(issues),
                dry_run=True,
            )

        snapshot = self._snapshot(rendered)
        applied: list[str] = []
        try:
            for service in enabled:
                for item in rendered.get(service.name, []):
                    self.runtime.fs.atomic_write(item.path, item.content, mode=item.mode)
                    if item.owner is not None:
                        try:
                            self.runtime.fs.chown(item.path, item.owner, item.group)
                        except (KeyError, OSError) as exc:
                            self.log.warning(
                                "could not chown %s to %s: %s", item.path, item.owner, exc
                            )
                    if item.sensitive:
                        self.runtime.fs.chmod(item.path, item.mode)
        except Exception as exc:
            self.log.exception("writing rendered files failed")
            self._restore(snapshot)
            return ApplyResult(
                ok=False,
                message=f"writing config failed: {exc}; rolled back",
                snapshot_id=snapshot.id,
                issues=tuple(issues),
                rolled_back=True,
            )

        if reload_services:
            for service in enabled:
                try:
                    result = service.apply()
                except Exception as exc:
                    result = ActionResult.failure(f"{service.name} crashed: {exc}")
                if not result.ok:
                    self.log.error("apply of %s failed: %s", service.name, result.message)
                    self._restore(snapshot)
                    self._reload_previous(enabled)
                    return ApplyResult(
                        ok=False,
                        message=(
                            f"{service.name} failed to apply: {result.message}; "
                            "configuration rolled back"
                        ),
                        snapshot_id=snapshot.id,
                        applied=tuple(applied),
                        failed=(service.name,),
                        issues=tuple(issues),
                        rolled_back=True,
                    )
                applied.append(service.name)

        for service in enabled:
            try:
                service.runtime.db.record_service_state(
                    service.name,
                    installed=True,
                    enabled=service.enabled,
                )
            except Exception:
                self.log.warning("could not record service state for %s", service.name)
        self.runtime.bus.emit(
            "config.applied",
            {"modules": applied, "snapshot": snapshot.id},
            source="apply",
        )
        return ApplyResult(
            ok=True,
            message=f"applied {len(applied)} modules",
            snapshot_id=snapshot.id,
            applied=tuple(applied),
            issues=tuple(issues),
        )

    def _reload_previous(self, services: list[ServiceModule]) -> None:
        self.runtime.systemd.daemon_reload()
        for service in services:
            try:
                service.restart()
            except Exception:
                self.log.exception("could not restart %s after rollback", service.name)

    # -- snapshots -----------------------------------------------------
    def _snapshot(self, rendered: dict[str, list[RenderedFile]]) -> Snapshot:
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        snapshot_id = f"{stamp}-{secrets.token_hex(3)}"
        root = self.snapshots_dir / snapshot_id
        files_dir = root / "files"
        self.runtime.fs.mkdir(files_dir, mode=0o700)
        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        index = 0
        for items in rendered.values():
            for item in items:
                path = Path(item.path)
                if path in seen:
                    continue
                seen.add(path)
                entry: dict[str, Any] = {"path": str(path), "existed": False}
                if self.runtime.fs.exists(path):
                    backup_name = f"{index:04d}.bak"
                    content = self.runtime.fs.read_text(path)
                    self.runtime.fs.atomic_write(
                        files_dir / backup_name, content, mode=0o600
                    )
                    try:
                        mode = self.runtime.fs.mode_of(path)
                    except OSError:
                        mode = item.mode
                    entry.update(
                        existed=True,
                        backup=backup_name,
                        mode=mode,
                    )
                    index += 1
                entries.append(entry)
        manifest = root / "manifest.json"
        payload = {
            "id": snapshot_id,
            "created_at": datetime.now().astimezone().isoformat(),
            "files": entries,
        }
        self.runtime.fs.atomic_write(
            manifest, json.dumps(payload, indent=2) + "\n", mode=0o600
        )
        snapshot = Snapshot(
            id=snapshot_id,
            root=root,
            manifest=manifest,
            created_at=str(payload["created_at"]),
            files=tuple(Path(e["path"]) for e in entries),
        )
        self.log.info("snapshot %s created for %d files", snapshot_id, len(entries))
        return snapshot

    def _restore(self, snapshot: Snapshot) -> bool:
        try:
            payload = json.loads(self.runtime.fs.read_text(snapshot.manifest))
        except (OSError, ValueError):
            self.log.exception("could not read snapshot manifest %s", snapshot.manifest)
            return False
        ok = True
        for entry in payload.get("files", []):
            path = Path(entry["path"])
            try:
                if entry.get("existed"):
                    content = self.runtime.fs.read_text(
                        snapshot.root / "files" / entry["backup"]
                    )
                    self.runtime.fs.atomic_write(
                        path, content, mode=int(entry.get("mode", 0o644))
                    )
                else:
                    self.runtime.fs.remove(path, missing_ok=True)
            except OSError:
                ok = False
                self.log.exception("could not restore %s", path)
        self.runtime.systemd.daemon_reload()
        if ok:
            self.log.info("snapshot %s restored", snapshot.id)
        self.runtime.bus.emit(
            "config.rolled_back", {"snapshot": snapshot.id}, source="apply"
        )
        return ok

    def restore_snapshot(self, snapshot_id: str) -> ActionResult:
        if not _SNAPSHOT_RE.match(snapshot_id):
            return ActionResult.failure("invalid snapshot id")
        snapshot = Snapshot(
            id=snapshot_id,
            root=self.snapshots_dir / snapshot_id,
            manifest=self.snapshots_dir / snapshot_id / "manifest.json",
            created_at="",
            files=(),
        )
        if not self.runtime.fs.exists(snapshot.manifest):
            return ActionResult.failure(f"snapshot not found: {snapshot_id}")
        ok = self._restore(snapshot)
        return ActionResult(
            ok=ok,
            message="snapshot restored" if ok else "snapshot restore had errors",
        )

    def list_snapshots(self, limit: int = 20) -> list[Snapshot]:
        if not self.runtime.fs.exists(self.snapshots_dir):
            return []
        result: list[Snapshot] = []
        for name in self.runtime.fs.listdir(self.snapshots_dir):
            path = self.snapshots_dir / name
            manifest = path / "manifest.json"
            if not self.runtime.fs.exists(manifest):
                continue
            try:
                payload = json.loads(self.runtime.fs.read_text(manifest))
            except (OSError, ValueError):
                continue
            result.append(
                Snapshot(
                    id=name,
                    root=path,
                    manifest=manifest,
                    created_at=str(payload.get("created_at", "")),
                    files=tuple(Path(e["path"]) for e in payload.get("files", [])),
                )
            )
        result.sort(key=lambda snap: snap.created_at, reverse=True)
        return result[: max(0, limit)]

    def prune_snapshots(self, keep: int = 10) -> int:
        snapshots = self.list_snapshots(limit=10_000)
        removed = 0
        for snapshot in snapshots[keep:]:
            try:
                self.runtime.fs.rmtree(snapshot.root)
                removed += 1
            except OSError:
                self.log.warning("could not prune snapshot %s", snapshot.id)
        return removed


def apply_error_message(exc: EventHubError) -> str:
    return str(exc)

from __future__ import annotations

from pathlib import Path

from tests.support import FakeFileSystem, make_runtime

from eventhub.core.config import AppConfig
from eventhub.core.orchestrator import ApplyEngine
from eventhub.core.types import (
    ActionResult,
    IssueLevel,
    RenderedFile,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule


class DummyService(ServiceModule):
    name = "dummy"
    title = "Dummy"
    config_attr = "hotspot"
    systemd_units = ("dummy.service",)
    fail_apply = False
    render_error = False

    def render(self, config: AppConfig) -> list[RenderedFile]:
        if self.render_error:
            raise RuntimeError("render boom")
        return [
            RenderedFile(Path("/etc/eventhub/dummy.conf"), "value=old\n", mode=0o640),
        ]

    def apply(self) -> ActionResult:
        if self.fail_apply:
            return ActionResult.failure("reload failed")
        return ActionResult.success("dummy applied")


class InvalidService(DummyService):
    name = "invalid"

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        return [ValidationIssue(IssueLevel.ERROR, "bad config", "hotspot.ssid")]


def test_apply_writes_files_and_records_state(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    engine = ApplyEngine(runtime)
    result = engine.apply([DummyService(runtime)])
    assert result.ok
    assert result.applied == ("dummy",)
    assert runtime.fs.read_text("/etc/eventhub/dummy.conf") == "value=old\n"
    assert runtime.fs.mode_of("/etc/eventhub/dummy.conf") == 0o640
    assert result.snapshot_id is not None
    assert len(engine.list_snapshots()) == 1
    assert runtime.db.service_state("dummy") is not None


def test_apply_aborts_on_validation_error(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    result = ApplyEngine(runtime).apply([InvalidService(runtime)])
    assert not result.ok
    assert "validation" in result.message
    assert not runtime.fs.exists("/etc/eventhub/dummy.conf")


def test_apply_aborts_on_render_error(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    service = DummyService(runtime)
    service.render_error = True
    result = ApplyEngine(runtime).apply([service])
    assert not result.ok
    assert "render" in result.message
    assert not runtime.fs.exists("/etc/eventhub/dummy.conf")


def test_apply_rolls_back_on_service_failure(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.fs.atomic_write("/etc/eventhub/dummy.conf", "original\n", mode=0o600)
    service = DummyService(runtime)
    service.fail_apply = True
    result = ApplyEngine(runtime).apply([service])
    assert not result.ok
    assert result.rolled_back
    assert runtime.fs.read_text("/etc/eventhub/dummy.conf") == "original\n"
    assert runtime.fs.mode_of("/etc/eventhub/dummy.conf") == 0o600
    assert runtime.systemd.daemon_reloads >= 1


def test_apply_removes_file_that_did_not_exist_on_rollback(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    service = DummyService(runtime)
    service.fail_apply = True
    result = ApplyEngine(runtime).apply([service])
    assert result.rolled_back
    assert not runtime.fs.exists("/etc/eventhub/dummy.conf")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    result = ApplyEngine(runtime).apply([DummyService(runtime)], dry_run=True)
    assert result.ok
    assert result.dry_run
    assert not runtime.fs.exists("/etc/eventhub/dummy.conf")
    assert not runtime.fs.exists(runtime.config.state_dir / "snapshots")


def test_restore_snapshot(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.fs.atomic_write("/etc/eventhub/dummy.conf", "original\n", mode=0o600)
    engine = ApplyEngine(runtime)
    result = engine.apply([DummyService(runtime)])
    assert result.ok
    assert runtime.fs.read_text("/etc/eventhub/dummy.conf") == "value=old\n"
    assert result.snapshot_id is not None
    restored = engine.restore_snapshot(result.snapshot_id)
    assert restored.ok
    assert runtime.fs.read_text("/etc/eventhub/dummy.conf") == "original\n"


def test_restore_unknown_snapshot(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    result = ApplyEngine(runtime).restore_snapshot("../oops")
    assert not result.ok


def test_disabled_modules_do_not_block_apply(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    service = InvalidService(runtime)
    service.config.hotspot.enabled = False
    result = ApplyEngine(runtime).apply([service])
    assert result.ok
    assert any(issue.level is IssueLevel.WARNING for issue in result.issues)


def test_render_relative_path_rejected(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    service = DummyService(runtime)

    def bad_render(config: AppConfig) -> list[RenderedFile]:
        return [RenderedFile(Path("relative.conf"), "x")]

    service.render = bad_render  # type: ignore[method-assign]
    result = ApplyEngine(runtime).apply([service])
    assert not result.ok
    assert any("relative" in issue.message for issue in result.issues)


def test_fake_filesystem_semantics() -> None:
    fs = FakeFileSystem()
    fs.atomic_write("/a/b/c.txt", "hello")
    assert fs.is_file("/a/b/c.txt")
    assert fs.listdir("/a/b") == ["c.txt"]
    assert fs.mode_of("/a/b/c.txt") == 0o644
    fs.rename("/a/b/c.txt", "/a/d/e.txt")
    assert not fs.exists("/a/b/c.txt")
    assert fs.read_text("/a/d/e.txt") == "hello"
    fs.rmtree("/a")
    assert not fs.exists("/a/b")

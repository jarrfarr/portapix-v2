"""Mount table helpers shared by storage and events."""

from __future__ import annotations

from pathlib import Path

from eventhub.core.adapters import FileSystem


def read_mounts(fs: FileSystem) -> list[tuple[str, str, str]]:
    try:
        text = fs.read_text("/proc/self/mounts")
    except (OSError, FileNotFoundError):
        return []
    mounts: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((parts[0], parts[1], parts[2]))
    return mounts


def is_mounted(fs: FileSystem, path: Path | str) -> bool:
    target = str(Path(path))
    return any(mountpoint == target for _device, mountpoint, _fstype in read_mounts(fs))


def mount_for(fs: FileSystem, path: Path | str) -> tuple[str, str, str] | None:
    target = str(Path(path))
    for mount in read_mounts(fs):
        if mount[1] == target:
            return mount
    return None

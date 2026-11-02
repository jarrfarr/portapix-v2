"""Shared system metrics used by the helper, dashboard and metrics daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from eventhub.core.context import Runtime


def _read(fs: Any, path: Path) -> str:
    try:
        return cast(str, fs.read_text(path).strip())
    except (OSError, FileNotFoundError):
        return ""


def collect_metrics(runtime: Runtime) -> dict[str, Any]:
    fs = runtime.fs
    metrics: dict[str, Any] = {}
    raw = _read(fs, Path("/sys/class/thermal/thermal_zone0/temp"))
    try:
        metrics["cpu_temp_c"] = round(int(raw) / 1000.0, 1)
    except ValueError:
        metrics["cpu_temp_c"] = None
    try:
        metrics["uptime_seconds"] = int(float(fs.read_text("/proc/uptime").split()[0]))
    except (OSError, ValueError, IndexError):
        metrics["uptime_seconds"] = None
    try:
        parts = fs.read_text("/proc/loadavg").split()
        metrics["load"] = [float(parts[0]), float(parts[1]), float(parts[2])]
    except (OSError, ValueError, IndexError):
        metrics["load"] = None
    try:
        meminfo: dict[str, int] = {}
        for line in fs.read_text("/proc/meminfo").splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0]) * 1024
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        metrics["memory"] = {
            "total_bytes": total,
            "available_bytes": available,
            "used_percent": round((total - available) * 100 / total, 1) if total else None,
        }
    except (OSError, ValueError):
        metrics["memory"] = None
    if runtime.runner.which("vcgencmd") is not None:
        result = runtime.runner.run(["vcgencmd", "get_throttled"], timeout=5)
        if result.ok and "=" in result.stdout:
            try:
                raw_value = int(result.stdout.strip().split("=")[1], 16)
                metrics["throttled"] = {
                    "raw": hex(raw_value),
                    "undervoltage": bool(raw_value & 0x1),
                    "frequency_capped": bool(raw_value & 0x2),
                    "throttled": bool(raw_value & 0x4),
                    "soft_temperature_limit": bool(raw_value & 0x8),
                }
            except ValueError:
                metrics["throttled"] = None
        else:
            metrics["throttled"] = None
    else:
        metrics["throttled"] = None
    metrics["ups"] = _ups_metrics(runtime)
    return metrics


def _ups_metrics(runtime: Runtime) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    base = Path("/sys/class/power_supply")
    try:
        names = runtime.fs.listdir(base)
    except OSError:
        return result
    for name in names:
        path = base / name
        capacity = _read(runtime.fs, path / "capacity")
        if not capacity:
            continue
        try:
            percent = int(capacity)
        except ValueError:
            continue
        result.append(
            {
                "name": name,
                "capacity_percent": percent,
                "status": _read(runtime.fs, path / "status"),
            }
        )
    return result

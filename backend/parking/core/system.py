"""What a machine reports about itself for the admin alerts (notifications.md §5.1, P8.7):
how full a filesystem is and the CPU temperature. No dependencies; every value is None where
the machine can't say (a cloud VM has no thermal zone)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

THERMAL = Path("/sys/class/thermal")


@dataclass(frozen=True)
class SystemStats:
    disk_pct: float | None = None  # used share of the filesystem, 0–100
    cpu_temp_c: float | None = None  # the hottest thermal zone


def disk_pct(path: Path) -> float | None:
    """Used share (0–100, like `df`: of what a normal user may use) of the filesystem holding
    `path`; None when it can't be read."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    reachable = usage.used + usage.free
    return round(100 * usage.used / reachable, 1) if reachable > 0 else None


def cpu_temp_c(thermal: Path = THERMAL) -> float | None:
    """The hottest `thermal_zone*/temp` in °C; None on machines without one."""
    temps = []
    for file in sorted(thermal.glob("thermal_zone*/temp")):
        try:
            temps.append(int(file.read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    return round(max(temps), 1) if temps else None


def system_stats(path: Path, thermal: Path = THERMAL) -> SystemStats:
    return SystemStats(disk_pct(path), cpu_temp_c(thermal))

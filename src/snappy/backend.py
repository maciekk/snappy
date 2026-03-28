"""Backend for querying snapper and btrfs."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class SnapperConfig:
    name: str
    subvolume: str


@dataclass
class Snapshot:
    number: int
    type: str  # "single", "pre", "post"
    date: str
    user: str
    used_space: str
    cleanup: str
    description: str
    userdata: dict[str, str] = field(default_factory=dict)
    pre_number: int | None = None
    post_number: int | None = None
    read_only: bool = True


@dataclass
class FilesystemUsage:
    device_size: int  # bytes
    device_allocated: int
    used: int
    free_estimated: int
    data_ratio: float
    metadata_ratio: float
    data_size: int = 0
    data_used: int = 0


@dataclass
class FileInfo:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    permissions: str


@dataclass
class FileInSnapshot:
    snapshot_number: int
    snapshot_date: str
    size: int
    mtime: float
    exists: bool


def is_root() -> bool:
    return os.geteuid() == 0


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Command timed out")


def _run_privileged(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command, using sudo if not already root."""
    if os.geteuid() == 0:
        return _run(cmd, timeout)
    return _run(["sudo"] + cmd, timeout)


def get_configs() -> list[SnapperConfig]:
    result = _run(["snapper", "--jsonout", "list-configs"])
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    return [SnapperConfig(name=c["config"], subvolume=c["subvolume"]) for c in data.get("configs", [])]


def get_snapshots(config_name: str) -> list[Snapshot]:
    result = _run_privileged(["snapper", "--jsonout", "-c", config_name, "list"])
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    snapshots = []
    for s in data.get("snapshots", []):
        userdata = s.get("userdata", {})
        if isinstance(userdata, str):
            userdata = {}
        snapshots.append(Snapshot(
            number=s.get("number", 0),
            type=s.get("type", "unknown"),
            date=s.get("date", ""),
            user=s.get("user", ""),
            used_space=s.get("used-space", ""),
            cleanup=s.get("cleanup", ""),
            description=s.get("description", ""),
            userdata=userdata,
            pre_number=s.get("pre-number") or None,
            post_number=s.get("post-number") or None,
            read_only=s.get("read-only", True),
        ))
    return snapshots


def get_config_details(config_name: str) -> dict[str, str]:
    result = _run_privileged(["snapper", "--jsonout", "-c", config_name, "get-config"])
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout)


def _parse_size(s: str) -> int:
    """Parse a human-readable size string like '1.50GiB' to bytes."""
    s = s.strip()
    if not s:
        return 0
    multipliers = {
        "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
        "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)].strip()) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def get_filesystem_usage(mount_point: str = "/") -> FilesystemUsage | None:
    result = _run(["btrfs", "filesystem", "usage", mount_point])
    if result.returncode != 0:
        return None
    text = result.stdout
    usage = FilesystemUsage(
        device_size=0, device_allocated=0, used=0,
        free_estimated=0, data_ratio=1.0, metadata_ratio=1.0,
    )
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Device size:"):
            usage.device_size = _parse_size(line.split(":", 1)[1])
        elif line.startswith("Device allocated:"):
            usage.device_allocated = _parse_size(line.split(":", 1)[1])
        elif line.startswith("Used:"):
            usage.used = _parse_size(line.split(":", 1)[1])
        elif line.startswith("Free (estimated):"):
            val = line.split(":", 1)[1].strip()
            # May contain "(min: ...)" — take the first value
            val = val.split("(")[0].strip()
            usage.free_estimated = _parse_size(val)
        elif line.startswith("Data ratio:"):
            try:
                usage.data_ratio = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("Metadata ratio:"):
            try:
                usage.metadata_ratio = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("Data,"):
            # e.g. "Data,single: Size:897.48GiB, Used:864.21GiB (96.29%)"
            m = re.search(r"Size:([\d.]+\S+),\s*Used:([\d.]+\S+)", line)
            if m:
                usage.data_size = _parse_size(m.group(1))
                usage.data_used = _parse_size(m.group(2))
    return usage


def get_snapshot_path(config: SnapperConfig, snapshot_number: int) -> Path:
    base = Path(config.subvolume) / ".snapshots" / str(snapshot_number) / "snapshot"
    return base


def browse_directory(path: str | Path) -> list[FileInfo]:
    path = Path(path)
    entries = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat(follow_symlinks=False)
                entries.append(FileInfo(
                    name=entry.name,
                    path=str(Path(entry.path)),
                    is_dir=entry.is_dir(follow_symlinks=False),
                    size=stat.st_size if not entry.is_dir(follow_symlinks=False) else 0,
                    mtime=stat.st_mtime,
                    permissions=oct(stat.st_mode)[-3:],
                ))
            except (PermissionError, OSError):
                entries.append(FileInfo(
                    name=entry.name,
                    path=str(Path(entry.path)),
                    is_dir=entry.is_dir(follow_symlinks=False),
                    size=0,
                    mtime=0,
                    permissions="???",
                ))
    except (PermissionError, OSError):
        pass
    return entries


def find_file_in_snapshots(config: SnapperConfig, relative_path: str) -> list[FileInSnapshot]:
    """Find a file across all snapshots of a config."""
    snapshots = get_snapshots(config.name)
    results = []
    for snap in snapshots:
        if snap.number == 0:
            continue
        snap_path = get_snapshot_path(config, snap.number) / relative_path
        if snap_path.exists():
            try:
                stat = snap_path.stat(follow_symlinks=False)
                results.append(FileInSnapshot(
                    snapshot_number=snap.number,
                    snapshot_date=snap.date,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    exists=True,
                ))
            except (PermissionError, OSError):
                results.append(FileInSnapshot(
                    snapshot_number=snap.number,
                    snapshot_date=snap.date,
                    size=0,
                    mtime=0,
                    exists=True,
                ))
        else:
            results.append(FileInSnapshot(
                snapshot_number=snap.number,
                snapshot_date=snap.date,
                size=0,
                mtime=0,
                exists=False,
            ))

    # Also check current (live) filesystem
    live_path = Path(config.subvolume) / relative_path
    if live_path.exists():
        try:
            stat = live_path.stat(follow_symlinks=False)
            results.append(FileInSnapshot(
                snapshot_number=-1,  # -1 means "live"
                snapshot_date="(current)",
                size=stat.st_size,
                mtime=stat.st_mtime,
                exists=True,
            ))
        except (PermissionError, OSError):
            pass

    return results


def delete_snapshot(config_name: str, snapshot_number: int) -> tuple[bool, str]:
    result = _run_privileged(["snapper", "-c", config_name, "delete", str(snapshot_number)], timeout=120)
    if result.returncode == 0:
        return True, f"Snapshot {snapshot_number} deleted."
    return False, result.stderr.strip() or f"Failed to delete snapshot {snapshot_number}"

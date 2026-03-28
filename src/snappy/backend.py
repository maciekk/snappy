"""Backend for querying snapper and btrfs."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ── Caching ────────────────────────────────────────────────────────────
#
# Principle: any operation that is slow (i.e. involves a privileged
# subprocess touching snapshot metadata) caches its result here.
# Callers always go through the public get_* functions; the cache is
# transparent to them.  Call invalidate_cache() after any mutating
# operation (e.g. delete) so subsequent reads are fresh.

_snapshot_cache: dict[str, list] = {}      # config_name -> list[Snapshot]
_config_detail_cache: dict[str, dict] = {} # config_name -> dict[str, str]


def invalidate_cache(config_name: str | None = None) -> None:
    """Invalidate cached data for one config, or all configs if None."""
    if config_name is None:
        _snapshot_cache.clear()
        _config_detail_cache.clear()
        log.debug("Cache invalidated (all configs)")
    else:
        _snapshot_cache.pop(config_name, None)
        _config_detail_cache.pop(config_name, None)
        log.debug("Cache invalidated for config '%s'", config_name)


# ── Sudo tracking ──────────────────────────────────────────────────────

_sudo_last_confirmed: float = 0.0  # monotonic timestamp of last successful sudo check
_sudo_timeout: int = 0  # configured sudo timeout in seconds (0 = unknown)


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


def get_sudo_timeout() -> int:
    """Query the configured sudo timestamp_timeout (seconds). Returns 0 if unknown."""
    global _sudo_timeout
    if _sudo_timeout > 0:
        return _sudo_timeout
    try:
        result = subprocess.run(
            ["sudo", "-n", "-l"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            # Look for "timestamp_timeout" in sudo -l output
            if "timestamp_timeout" in line:
                # e.g. "    timestamp_timeout    5"  or  "timestamp_timeout=5"
                for token in re.split(r"[=\s]+", line):
                    try:
                        val = float(token)
                        _sudo_timeout = int(val * 60)  # sudo uses minutes
                        log.info("Detected sudo timeout: %d seconds", _sudo_timeout)
                        return _sudo_timeout
                    except ValueError:
                        continue
    except Exception:
        pass
    # Default: 5 minutes is the sudo default
    _sudo_timeout = 300
    log.info("Using default sudo timeout: %d seconds", _sudo_timeout)
    return _sudo_timeout


def check_sudo() -> bool:
    """Check if sudo credentials are currently cached (non-interactive)."""
    global _sudo_last_confirmed
    if is_root():
        return True
    result = subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, timeout=5,
    )
    if result.returncode == 0:
        _sudo_last_confirmed = time.monotonic()
        return True
    return False


def sudo_seconds_remaining() -> int | None:
    """Estimate seconds until sudo credentials expire.

    Returns None if running as root (no expiry), or if we have never
    confirmed sudo.  Returns 0 if already expired.
    """
    if is_root():
        return None
    if _sudo_last_confirmed == 0.0:
        return 0
    timeout = get_sudo_timeout()
    elapsed = time.monotonic() - _sudo_last_confirmed
    remaining = max(0, int(timeout - elapsed))
    return remaining


class SudoExpiredError(Exception):
    """Raised when a privileged command cannot run because sudo has expired."""


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    log.debug("Running: %s", cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("Command timed out after %ds: %s", timeout, cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Command timed out")
    if result.returncode != 0:
        log.warning("Command failed (rc=%d): %s\nstderr: %s", result.returncode, cmd, result.stderr.strip())
    else:
        log.debug("Command succeeded: %s (stdout %d bytes)", cmd, len(result.stdout))
    return result


def _run_privileged(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command, using sudo if not already root.

    Raises SudoExpiredError if sudo credentials have expired.
    """
    global _sudo_last_confirmed
    if os.geteuid() == 0:
        return _run(cmd, timeout)
    # Quick non-interactive check before attempting the real command
    if not check_sudo():
        log.warning("sudo credentials expired before running: %s", cmd)
        raise SudoExpiredError("sudo credentials have expired")
    result = _run(["sudo"] + cmd, timeout)
    if result.returncode == 0:
        _sudo_last_confirmed = time.monotonic()
    return result


def get_configs() -> list[SnapperConfig]:
    result = _run(["snapper", "--jsonout", "list-configs"])
    if result.returncode != 0:
        log.error("Failed to list snapper configs")
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Invalid JSON from snapper list-configs: %s", result.stdout[:200])
        return []
    configs = [SnapperConfig(name=c["config"], subvolume=c["subvolume"]) for c in data.get("configs", [])]
    # Sort: root subvolume "/" first, then alphabetically by subvolume
    configs.sort(key=lambda c: (c.subvolume != "/", c.subvolume))
    log.info("Found %d snapper configs: %s", len(configs), [c.name for c in configs])
    return configs


def get_snapshots(config_name: str) -> list[Snapshot]:
    if config_name in _snapshot_cache:
        log.debug("Cache hit: snapshots for '%s'", config_name)
        return _snapshot_cache[config_name]
    result = _run_privileged(["snapper", "--jsonout", "-c", config_name, "list"])
    if result.returncode != 0:
        log.error("Failed to list snapshots for config '%s'", config_name)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Invalid JSON from snapper list (config '%s'): %s", config_name, result.stdout[:200])
        return []

    # Snapper's JSON shape varies by version:
    #   - older: {"snapshots": [...]}
    #   - newer: direct list [...]
    #   - some: {<config_name>: [...]}
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        raw_list = (
            data.get("snapshots")
            or data.get(config_name)
            or next((v for v in data.values() if isinstance(v, list)), None)
            or []
        )
        log.debug("snapper list JSON keys for '%s': %s", config_name, list(data.keys()))
    else:
        log.error("Unexpected JSON type from snapper list (config '%s'): %s", config_name, type(data))
        return []

    log.debug("snapper list raw entry count for '%s': %d", config_name, len(raw_list))
    snapshots: list[Snapshot] = []
    for s in raw_list:
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
    _snapshot_cache[config_name] = snapshots
    log.debug("Cached %d snapshots for '%s'", len(snapshots), config_name)
    return snapshots


def get_config_details(config_name: str) -> dict[str, str]:
    if config_name in _config_detail_cache:
        log.debug("Cache hit: config details for '%s'", config_name)
        return _config_detail_cache[config_name]
    result = _run_privileged(["snapper", "--jsonout", "-c", config_name, "get-config"])
    if result.returncode != 0:
        log.error("Failed to get config details for '%s'", config_name)
        return {}
    try:
        details = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Invalid JSON from snapper get-config (config '%s'): %s", config_name, result.stdout[:200])
        return {}
    _config_detail_cache[config_name] = details
    return details


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


def find_file_in_snapshots(
    config: SnapperConfig,
    relative_path: str,
    snapshots: list[Snapshot],
) -> list[FileInSnapshot]:
    """Find a file across all snapshots of a config.

    Callers must supply the already-fetched snapshot list to avoid a redundant
    slow sudo call.
    """
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
        invalidate_cache(config_name)
        return True, f"Snapshot {snapshot_number} deleted."
    return False, result.stderr.strip() or f"Failed to delete snapshot {snapshot_number}"

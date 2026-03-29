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

_snapshot_cache: dict[str, list] = {}          # config_name -> list[Snapshot]
_config_detail_cache: dict[str, dict] = {}     # config_name -> dict[str, str]
_dir_size_cache: dict[str, int] = {}           # abs path -> total bytes (from du -sb)
_status_cache: dict[tuple, dict[str, str]] = {}  # (config_name, snap_num) -> {rel_path -> status_char}


def invalidate_cache(config_name: str | None = None) -> None:
    """Invalidate cached data for one config, or all configs if None."""
    if config_name is None:
        _snapshot_cache.clear()
        _config_detail_cache.clear()
        _dir_size_cache.clear()
        _status_cache.clear()
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


@dataclass
class FileSearchMatch:
    snapshot_number: int
    snapshot_date: str
    path: str
    size: int
    mtime: float


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
    """List directory contents using privileged access (sudo if needed)."""
    path = Path(path)
    result = _run_privileged([
        "find", str(path), "-maxdepth", "1", "-mindepth", "1",
        "-printf", r"%f\t%y\t%s\t%T@\t%m\n",
    ])
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, type_char, size_str, mtime_str, perms = parts
        try:
            size = int(size_str)
            mtime = float(mtime_str)
        except ValueError:
            size, mtime = 0, 0.0
        is_dir = type_char == "d"
        entries.append(FileInfo(
            name=name,
            path=str(path / name),
            is_dir=is_dir,
            size=size if not is_dir else 0,
            mtime=mtime,
            permissions=perms,
        ))
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def get_dir_size(path: str | Path) -> int:
    """Return total disk usage of a directory tree in bytes (via sudo du -sb).

    Results are cached by absolute path for the lifetime of the process.
    Raises SudoExpiredError if sudo credentials have lapsed.
    """
    path_str = str(Path(path).resolve())
    if path_str in _dir_size_cache:
        log.debug("Cache hit: dir size for '%s'", path_str)
        return _dir_size_cache[path_str]
    result = _run_privileged(["du", "-sb", path_str])
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                size = int(parts[0])
                _dir_size_cache[path_str] = size
                log.debug("Dir size %s: %d bytes", path_str, size)
                return size
            except ValueError:
                continue
    log.warning("du produced no parseable output for '%s': %s", path_str, result.stderr.strip())
    return 0


def get_cached_dir_size(path: str | Path) -> int | None:
    """Return the cached du size for path, or None if not yet computed."""
    return _dir_size_cache.get(str(Path(path).resolve()))


def get_snapshot_status(config_name: str, snap_num: int) -> dict[str, str]:
    """Return a mapping of paths that differ between snap_num and the current filesystem.

    Keys are paths relative to the subvolume root (e.g. '/etc/fstab').
    Values are the first character of snapper's status code:
      '-'  file is in the snapshot but was deleted from the current filesystem
      'c'  content changed (file exists in both but differs)
      '+'  file was added to the current filesystem after the snapshot (won't
           appear in the snapshot tree, included for completeness)
      other letters indicate permission/owner/type changes

    Runs: snapper -c <config_name> status <snap_num>..0
    Output lines look like: 'c..... /etc/fstab'
    Results are cached; snapshots are immutable so the cache never expires.
    Raises SudoExpiredError if credentials have lapsed.
    """
    key = (config_name, snap_num)
    if key in _status_cache:
        log.debug("Cache hit: status for %s #%d", config_name, snap_num)
        return _status_cache[key]
    result = _run_privileged(["snapper", "-c", config_name, "status", f"{snap_num}..0"])
    statuses: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # Format: 6 status chars, space, absolute path from subvolume root
        parts = line.split(None, 1)
        if len(parts) == 2:
            status_code, path = parts
            statuses[path] = status_code[0] if status_code else "?"
    log.debug("Status for %s #%d: %d changed paths", config_name, snap_num, len(statuses))
    _status_cache[key] = statuses
    return statuses


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


def search_files_in_snapshots(
    config: SnapperConfig,
    pattern: str,
    snapshots: list[Snapshot],
) -> list[FileSearchMatch]:
    """Find files whose path contains *pattern* (substring) across all snapshots.

    Uses a single privileged ``find`` invocation across the entire .snapshots
    directory so we only pay the sudo overhead once.
    """
    snap_dir = Path(config.subvolume) / ".snapshots"
    snap_by_number = {s.number: s for s in snapshots if s.number != 0}

    result = _run_privileged([
        "find", str(snap_dir),
        "-path", f"*/snapshot/*{pattern}*",
        "-printf", r"%s\t%T@\t%p\n",
    ])

    results: list[FileSearchMatch] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            size = int(parts[0])
            mtime = float(parts[1])
            abs_path = Path(parts[2])
        except ValueError:
            continue

        try:
            rel_to_snap_dir = abs_path.relative_to(snap_dir)
        except ValueError:
            continue

        rel_parts = rel_to_snap_dir.parts
        # Expected structure: {num}/snapshot/{rel/path}
        if len(rel_parts) < 3 or rel_parts[1] != "snapshot":
            continue

        try:
            snap_num = int(rel_parts[0])
        except ValueError:
            continue

        snap = snap_by_number.get(snap_num)
        if snap is None:
            continue

        rel_path = str(Path(*rel_parts[2:]))
        results.append(FileSearchMatch(
            snapshot_number=snap_num,
            snapshot_date=snap.date,
            path=rel_path,
            size=size,
            mtime=mtime,
        ))

    return results


def delete_snapshot(config_name: str, snapshot_number: int) -> tuple[bool, str]:
    result = _run_privileged(["snapper", "-c", config_name, "delete", str(snapshot_number)], timeout=120)
    if result.returncode == 0:
        invalidate_cache(config_name)
        return True, f"Snapshot {snapshot_number} deleted."
    return False, result.stderr.strip() or f"Failed to delete snapshot {snapshot_number}"

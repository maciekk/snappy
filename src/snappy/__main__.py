"""Entry point for snappy."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path


def _setup_logging() -> Path:
    """Configure file-based debug logging. Returns the log file path."""
    log_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "snappy"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "snappy.log"
    logging.basicConfig(
        filename=str(log_file),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return log_file


def _ensure_sudo() -> bool:
    """Prompt for sudo credentials before TUI takes over the terminal.

    Returns True if sudo credentials are available, False otherwise.
    """
    if os.geteuid() == 0:
        return True
    # Check if sudo credentials are already cached (non-interactive)
    result = subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, timeout=5,
    )
    if result.returncode == 0:
        return True
    # Credentials not cached — prompt interactively (before TUI starts)
    print("Snappy needs sudo for reading snapshots. You may be prompted for your password.")
    result = subprocess.run(["sudo", "-v"], timeout=60)
    return result.returncode == 0


def main() -> None:
    log_file = _setup_logging()
    log = logging.getLogger(__name__)
    log.info("Snappy starting")

    if not _ensure_sudo():
        print(
            "Warning: Could not obtain sudo credentials. Snapshot listing may fail.",
            file=sys.stderr,
        )
        log.warning("sudo credential caching failed — privileged commands will likely fail")

    # Record the initial sudo confirmation so the timer starts correctly
    from snappy import backend
    backend.check_sudo()
    backend.get_sudo_timeout()

    from snappy.app import SnappyApp

    log.info("Launching TUI (log file: %s)", log_file)
    app = SnappyApp()
    app.run()


if __name__ == "__main__":
    main()

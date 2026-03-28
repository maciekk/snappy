# Snappy

A terminal UI for browsing and managing [snapper](http://snapper.io/) Btrfs snapshots.

## Requirements

- Linux with Btrfs and snapper configured
- Python 3.14+
- `snapper` and `btrfs-progs` installed
- `sudo` access (required to run `snapper list` and other privileged commands)

## Installation

```bash
uv tool install .
```

Or to run directly from the source tree:

```bash
uv run snappy
```

## Usage

```bash
snappy
```

Snappy needs sudo to read snapshot metadata. If your sudo credentials are not
already cached, you will be prompted for your password before the TUI starts.
Once inside the TUI, a countdown in the status bar shows how long your sudo
session has remaining. If it expires, a popup will tell you to run `sudo -v`
in another terminal to refresh it.

### Key bindings

| Key | Action |
|-----|--------|
| `r` | Refresh active tab |
| `f` | File search — find a file across all snapshots |
| `b` / `Enter` | Browse the selected snapshot |
| `d` | Delete the selected snapshot |
| `q` | Quit |

### Tabs

Each snapper config appears as a tab. The root (`/`) config is always first.
Snapshot lists are loaded lazily — only the active tab loads on startup;
other tabs load on first switch.

## Debug log

Snappy writes a debug log to:

```
~/.local/state/snappy/snappy.log
```

(`$XDG_STATE_HOME/snappy/snappy.log` if that variable is set.)

All subprocess calls, exit codes, and error output are recorded there — check
it first when something seems silently broken.

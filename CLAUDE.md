# Snappy — development guidelines

## Performance principles

### Lazy execution
Any operation that is slow — typically those that invoke a privileged subprocess
touching snapshot metadata — must not run until the user actually needs the
result.

- Snapshot lists are loaded per-tab, on first activation, not all at startup.
- Refresh reloads only the active tab; other tabs reload on next activation.
- File search and browse operations are triggered by explicit user action.

### Caching
The output of any slow command that is likely to be needed again must be cached
at the backend level (`backend.py`).

- `get_snapshots()` and `get_config_details()` cache their results in
  module-level dicts (`_snapshot_cache`, `_config_detail_cache`).
- Callers always go through the public `get_*` functions; caching is
  transparent to them.
- Any mutating operation (e.g. `delete_snapshot`) must call
  `invalidate_cache(config_name)` on success so subsequent reads are fresh.
- The app calls `backend.invalidate_cache()` on full refresh.
- There is no separate caching layer in the frontend; `self._loaded_configs`
  tracks only which tab widgets have been built, not snapshot data.

## sudo

Snappy requires sudo to run `snapper list` and other privileged commands.
Because Textual owns the terminal, sudo cannot prompt for a password once the
TUI is running. Therefore:

- Credentials are obtained interactively before the TUI starts (`_ensure_sudo`
  in `__main__.py`).
- Every privileged backend call checks `sudo -n true` first and raises
  `SudoExpiredError` if credentials have lapsed.
- The app shows a countdown timer and a popup when credentials expire, telling
  the user to run `sudo -v` in another terminal.

## Logging

A debug log is written to `~/.local/state/snappy/snappy.log` (respects
`$XDG_STATE_HOME`). All subprocess invocations, their exit codes, and stderr
on failure are logged. Use this file first when diagnosing silent failures.

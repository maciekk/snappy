"""Snappy TUI application."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import humanize
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)

from snappy import backend


# ── Helpers ──────────────────────────────────────────────────────────────

def _fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "-"
    return humanize.naturalsize(size_bytes, binary=True)


def _fmt_mtime(ts: float) -> str:
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _pct(used: int, total: int) -> str:
    if total <= 0:
        return "?"
    return f"{used / total * 100:.1f}%"


# ── File Search Screen ───────────────────────────────────────────────────

class FileSearchScreen(ModalScreen):
    """Search for a file across all snapshots."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
    ]

    CSS = """
    FileSearchScreen {
        align: center middle;
    }
    #search-dialog {
        width: 90%;
        height: 85%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #search-input {
        margin-bottom: 1;
    }
    #search-results {
        height: 1fr;
    }
    #search-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #search-loading {
        height: 3;
        display: none;
    }
    """

    def __init__(self, config: backend.SnapperConfig) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label(f"File Search — config: [bold]{self.config.name}[/bold] ({self.config.subvolume})")
            yield Label(
                f"Enter path relative to {self.config.subvolume} (e.g. home/user/bigfile.tar)",
                id="search-hint",
            )
            yield Input(placeholder="relative/path/to/file", id="search-input")
            yield LoadingIndicator(id="search-loading")
            yield DataTable(id="search-results")

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.add_columns("Snapshot #", "Date", "Exists", "Size", "Modified")
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        relative_path = event.value.strip()
        if not relative_path:
            return
        self.query_one("#search-loading", LoadingIndicator).styles.display = "block"
        self._do_search(relative_path)

    @work(thread=True)
    def _do_search(self, relative_path: str) -> None:
        results = backend.find_file_in_snapshots(self.config, relative_path)
        self.app.call_from_thread(self._populate_results, results)

    def _populate_results(self, results: list[backend.FileInSnapshot]) -> None:
        self.query_one("#search-loading", LoadingIndicator).styles.display = "none"
        table = self.query_one("#search-results", DataTable)
        table.clear()
        for r in results:
            snap_label = "(live)" if r.snapshot_number == -1 else str(r.snapshot_number)
            exists_str = "Yes" if r.exists else "No"
            size_str = _fmt_size(r.size) if r.exists else "-"
            mtime_str = _fmt_mtime(r.mtime) if r.exists else "-"
            table.add_row(snap_label, r.snapshot_date, exists_str, size_str, mtime_str)


# ── Browse Snapshot Screen ───────────────────────────────────────────────

class BrowseScreen(ModalScreen):
    """Browse a snapshot's directory tree."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
    ]

    CSS = """
    BrowseScreen {
        align: center middle;
    }
    #browse-dialog {
        width: 95%;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #browse-layout {
        height: 1fr;
    }
    #browse-tree {
        width: 2fr;
    }
    #browse-detail {
        width: 1fr;
        border-left: tall $accent;
        padding: 0 1;
    }
    .detail-label {
        margin-bottom: 0;
    }
    """

    def __init__(self, snapshot_path: Path, snapshot_label: str) -> None:
        super().__init__()
        self.snapshot_path = snapshot_path
        self.snapshot_label = snapshot_label

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            yield Label(f"Browsing: [bold]{self.snapshot_label}[/bold]  ({self.snapshot_path})")
            with Horizontal(id="browse-layout"):
                if self.snapshot_path.is_dir():
                    yield DirectoryTree(str(self.snapshot_path), id="browse-tree")
                else:
                    yield Label(f"Path not accessible: {self.snapshot_path}", id="browse-tree")
                with Vertical(id="browse-detail"):
                    yield Static("Select a file to see details", id="detail-info")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._show_file_detail(Path(event.path))

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._show_file_detail(Path(event.path))

    def _show_file_detail(self, path: Path) -> None:
        detail = self.query_one("#detail-info", Static)
        try:
            stat = path.stat(follow_symlinks=False)
            lines = [
                f"[bold]Name:[/bold] {path.name}",
                f"[bold]Size:[/bold] {_fmt_size(stat.st_size)}",
                f"[bold]Modified:[/bold] {_fmt_mtime(stat.st_mtime)}",
                f"[bold]Permissions:[/bold] {oct(stat.st_mode)[-3:]}",
                f"[bold]Type:[/bold] {'Directory' if path.is_dir() else 'File'}",
            ]
            detail.update("\n".join(lines))
        except (PermissionError, OSError) as e:
            detail.update(f"Cannot stat: {e}")


# ── Delete Confirmation Screen ───────────────────────────────────────────

class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirm snapshot deletion."""

    BINDINGS = [
        Binding("y", "confirm", "Yes, delete"),
        Binding("n,escape", "cancel", "Cancel"),
    ]

    CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: 10;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, config_name: str, snapshot_number: int) -> None:
        super().__init__()
        self.config_name = config_name
        self.snapshot_number = snapshot_number

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Delete snapshot [bold]{self.snapshot_number}[/bold] from config [bold]{self.config_name}[/bold]?")
            yield Label("")
            yield Label("[y] Yes, delete    [n/Esc] Cancel")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── Main App ─────────────────────────────────────────────────────────────

class SnappyApp(App):
    """Btrfs snapshot analyzer TUI."""

    TITLE = "Snappy"
    SUB_TITLE = "Btrfs Snapshot Analyzer"

    CSS = """
    #fs-summary {
        height: auto;
        padding: 0 1;
        background: $boost;
        margin-bottom: 1;
    }
    #root-warning {
        background: $warning;
        color: $text;
        padding: 0 1;
        height: auto;
    }
    .config-tab {
        height: 1fr;
    }
    #snapshot-table {
        height: 1fr;
    }
    .status-bar {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #loading-container {
        height: auto;
        padding: 1 2;
        align: center middle;
    }
    #loading-indicator {
        height: 3;
    }
    #loading-text {
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "file_search", "File Search"),
        Binding("b,enter", "browse", "Browse Snapshot"),
        Binding("d", "delete_snapshot", "Delete Snapshot"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.configs: list[backend.SnapperConfig] = []
        self.snapshots: dict[str, list[backend.Snapshot]] = {}
        self.fs_usage: backend.FilesystemUsage | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        if not backend.is_root():
            yield Static(
                " Note: sudo will be used for privileged commands — you may be prompted for your password",
                id="root-warning",
            )
        yield Static("", id="fs-summary")
        with Vertical(id="loading-container"):
            yield LoadingIndicator(id="loading-indicator")
            yield Static("Reading snapshots — this can take a while...", id="loading-text")
        yield TabbedContent(id="config-tabs")
        yield Footer()

    def on_mount(self) -> None:
        self._load_data()

    @work(thread=True)
    def _load_data(self) -> None:
        configs = backend.get_configs()
        fs_usage = backend.get_filesystem_usage()
        snapshots = {}
        for cfg in configs:
            snapshots[cfg.name] = backend.get_snapshots(cfg.name)
        self.app.call_from_thread(self._populate, configs, snapshots, fs_usage)

    def _populate(
        self,
        configs: list[backend.SnapperConfig],
        snapshots: dict[str, list[backend.Snapshot]],
        fs_usage: backend.FilesystemUsage | None,
    ) -> None:
        self.configs = configs
        self.snapshots = snapshots
        self.fs_usage = fs_usage

        # Hide loading indicator
        try:
            self.query_one("#loading-container").remove()
        except Exception:
            pass

        # Filesystem summary
        summary = self.query_one("#fs-summary", Static)
        if fs_usage:
            summary.update(
                f"Disk: {_fmt_size(fs_usage.device_size)}  "
                f"Used: {_fmt_size(fs_usage.used)} ({_pct(fs_usage.used, fs_usage.device_size)})  "
                f"Free: {_fmt_size(fs_usage.free_estimated)}  "
                f"Allocated: {_fmt_size(fs_usage.device_allocated)}"
            )
        else:
            summary.update("Could not read filesystem usage (run as root?)")

        # Build tabs for each config
        tabs = self.query_one("#config-tabs", TabbedContent)
        for cfg in configs:
            pane = TabPane(f"{cfg.name} ({cfg.subvolume})", id=f"tab-{cfg.name}")
            tabs.add_pane(pane)

        # Populate tables after tabs are mounted — defer slightly
        self.set_timer(0.1, self._populate_tables)

    def _populate_tables(self) -> None:
        for cfg in self.configs:
            try:
                pane = self.query_one(f"#tab-{cfg.name}", TabPane)
            except Exception:
                continue

            table = DataTable(id=f"table-{cfg.name}")
            status = Static("", classes="status-bar", id=f"status-{cfg.name}")
            pane.mount(table)
            pane.mount(status)

            table.cursor_type = "row"
            table.add_columns("#", "Type", "Date", "Description", "Used Space", "Cleanup", "RO")

            snaps = self.snapshots.get(cfg.name, [])
            for snap in reversed(snaps):  # newest first
                if snap.number == 0:
                    # Snapshot 0 is the current subvolume, skip
                    continue
                ro_str = "yes" if snap.read_only else "no"
                table.add_row(
                    str(snap.number),
                    snap.type,
                    snap.date,
                    snap.description[:50],
                    snap.used_space or "-",
                    snap.cleanup,
                    ro_str,
                    key=str(snap.number),
                )

            count = len([s for s in snaps if s.number != 0])
            status_widget = self.query_one(f"#status-{cfg.name}", Static)
            status_widget.update(f"{count} snapshots")

    def _get_active_config(self) -> backend.SnapperConfig | None:
        tabs = self.query_one("#config-tabs", TabbedContent)
        active_id = tabs.active
        if not active_id:
            return self.configs[0] if self.configs else None
        # active_id is like "tab-root"
        config_name = active_id.replace("tab-", "")
        for cfg in self.configs:
            if cfg.name == config_name:
                return cfg
        return self.configs[0] if self.configs else None

    def _get_selected_snapshot_number(self) -> int | None:
        cfg = self._get_active_config()
        if not cfg:
            return None
        try:
            table = self.query_one(f"#table-{cfg.name}", DataTable)
        except Exception:
            return None
        if table.cursor_row is not None and table.row_count > 0:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return int(row_key.value)
        return None

    def action_refresh(self) -> None:
        # Clear and reload
        tabs = self.query_one("#config-tabs", TabbedContent)
        tabs.clear_panes()
        loading = Vertical(
            LoadingIndicator(id="loading-indicator"),
            Static("Reading snapshots — this can take a while...", id="loading-text"),
            id="loading-container",
        )
        self.mount(loading, before="#config-tabs")
        self._load_data()

    def action_file_search(self) -> None:
        cfg = self._get_active_config()
        if cfg:
            self.push_screen(FileSearchScreen(cfg))

    def action_browse(self) -> None:
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if cfg and snap_num:
            snap_path = backend.get_snapshot_path(cfg, snap_num)
            label = f"Config: {cfg.name}  Snapshot: #{snap_num}"
            self.push_screen(BrowseScreen(snap_path, label))

    def action_delete_snapshot(self) -> None:
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if cfg and snap_num:
            self.push_screen(
                ConfirmDeleteScreen(cfg.name, snap_num),
                callback=self._handle_delete,
            )

    def _handle_delete(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if cfg and snap_num:
            self._do_delete(cfg.name, snap_num)

    @work(thread=True)
    def _do_delete(self, config_name: str, snap_num: int) -> None:
        ok, msg = backend.delete_snapshot(config_name, snap_num)
        self.app.call_from_thread(self._on_delete_done, ok, msg)

    def _on_delete_done(self, ok: bool, msg: str) -> None:
        self.notify(msg, severity="information" if ok else "error")
        if ok:
            self.action_refresh()

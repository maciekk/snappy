"""Snappy TUI application."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import humanize
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from snappy import backend

log = logging.getLogger(__name__)


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


# ── Braille Spinner ──────────────────────────────────────────────────────

class BrailleSpinner(Widget):
    """Single-character animated braille spinner in orange."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    DEFAULT_CSS = """
    BrailleSpinner {
        width: auto;
        height: 1;
        color: orange;
    }
    """

    def on_mount(self) -> None:
        self.auto_refresh = 1 / 10

    def render(self) -> Text:
        from time import time
        frame = self._FRAMES[int(time() * 10) % len(self._FRAMES)]
        return Text(frame, style="bold orange")


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
        height: 1;
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
            yield BrailleSpinner(id="search-loading")
            yield DataTable(id="search-results")

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.add_columns("Snapshot #", "Date", "Exists", "Size", "Modified")
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        relative_path = event.value.strip()
        if not relative_path:
            return
        self.query_one("#search-loading", BrailleSpinner).styles.display = "block"
        self._do_search(relative_path)

    @work(thread=True)
    def _do_search(self, relative_path: str) -> None:
        try:
            snapshots = backend.get_snapshots(self.config.name)  # cache hit
            results = backend.find_file_in_snapshots(self.config, relative_path, snapshots)
        except backend.SudoExpiredError:
            self.app.call_from_thread(self._on_sudo_expired_search)
            return
        self.app.call_from_thread(self._populate_results, results)

    def _on_sudo_expired_search(self) -> None:
        self.query_one("#search-loading", BrailleSpinner).styles.display = "none"
        self.app.push_screen(SudoExpiredScreen())

    def _populate_results(self, results: list[backend.FileInSnapshot]) -> None:
        self.query_one("#search-loading", BrailleSpinner).styles.display = "none"
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


# ── Sudo Expired Screen ─────────────────────────────────────────────────

class SudoExpiredScreen(ModalScreen):
    """Inform the user that sudo has expired."""

    BINDINGS = [
        Binding("escape,enter", "dismiss", "OK"),
    ]

    CSS = """
    SudoExpiredScreen {
        align: center middle;
    }
    #sudo-dialog {
        width: 70;
        height: 12;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="sudo-dialog"):
            yield Label("[bold]sudo credentials have expired[/bold]")
            yield Label("")
            yield Label("Please run [bold]sudo -v[/bold] in another terminal to refresh,")
            yield Label("then press [bold]r[/bold] here to retry.")
            yield Label("")
            yield Label("[Enter/Esc] Dismiss")


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
    }
    #sudo-status {
        height: auto;
        padding: 0 1;
        background: $boost;
        margin-bottom: 1;
    }
    #sudo-status.sudo-ok {
        color: $success;
    }
    #sudo-status.sudo-warn {
        color: $warning;
    }
    #sudo-status.sudo-expired {
        color: $error;
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
        height: 1;
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
        self.fs_usage: backend.FilesystemUsage | None = None
        self._loaded_configs: set[str] = set()  # configs whose snapshots have been fetched

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="fs-summary")
        if not backend.is_root():
            yield Static("", id="sudo-status")
        with Vertical(id="loading-container"):
            yield BrailleSpinner(id="loading-indicator")
            yield Static("Reading snapshots — this can take a while...", id="loading-text")
        yield TabbedContent(id="config-tabs")
        yield Footer()

    def on_mount(self) -> None:
        self._load_data()
        if not backend.is_root():
            self._update_sudo_status()
            self.set_interval(15, self._update_sudo_status)

    @work(thread=True)
    def _update_sudo_status(self) -> None:
        """Update the sudo countdown in the status bar."""
        remaining = backend.sudo_seconds_remaining()
        # When estimate says expired or close, do a real check to confirm
        if remaining is not None and remaining <= 30:
            if backend.check_sudo():
                remaining = backend.sudo_seconds_remaining()
        self.app.call_from_thread(self._render_sudo_status, remaining)

    def _render_sudo_status(self, remaining: int | None) -> None:
        try:
            widget = self.query_one("#sudo-status", Static)
        except Exception:
            return
        if remaining is None:
            return
        if remaining == 0:
            widget.update("sudo: EXPIRED — run 'sudo -v' in another terminal, then press r")
            widget.set_classes("sudo-expired")
        elif remaining <= 60:
            widget.update(f"sudo: expires in {remaining}s — run 'sudo -v' in another terminal to refresh")
            widget.set_classes("sudo-warn")
        else:
            minutes = remaining // 60
            secs = remaining % 60
            widget.update(f"sudo: {minutes}m {secs:02d}s remaining")
            widget.set_classes("sudo-ok")

    @work(thread=True)
    def _load_data(self) -> None:
        """Fetch configs and filesystem usage only — snapshots are loaded lazily per tab."""
        configs = backend.get_configs()
        fs_usage = backend.get_filesystem_usage()
        self.app.call_from_thread(self._populate, configs, fs_usage)

    def _populate(
        self,
        configs: list[backend.SnapperConfig],
        fs_usage: backend.FilesystemUsage | None,
    ) -> None:
        self.configs = configs
        self._loaded_configs = set()
        self.fs_usage = fs_usage

        # Hide global loading indicator
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

        # Build tabs — each starts with a spinner until its snapshots are loaded
        tabs = self.query_one("#config-tabs", TabbedContent)
        for cfg in configs:
            pane = TabPane(f"{cfg.name} ({cfg.subvolume})", id=f"tab-{cfg.name}")
            tabs.add_pane(pane)

        # Mount per-tab spinners after panes are added, then load the active tab
        self.set_timer(0.1, self._init_tab_spinners)

    def _init_tab_spinners(self) -> None:
        """Add a spinner to every tab pane, then kick off loading for the active tab."""
        for cfg in self.configs:
            try:
                pane = self.query_one(f"#tab-{cfg.name}", TabPane)
                pane.mount(BrailleSpinner(id=f"spinner-{cfg.name}"))
                pane.mount(Static("Loading snapshots…", id=f"loading-label-{cfg.name}",
                                  classes="status-bar"))
            except Exception:
                pass
        # Load only the active tab now
        cfg = self._get_active_config()
        if cfg:
            self._load_tab_snapshots(cfg.name)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Load a tab's snapshots the first time it is activated."""
        if not event.tab:
            return
        config_name = event.tab.id.replace("tab-", "", 1) if event.tab.id else None
        if config_name and config_name not in self._loaded_configs:
            self._load_tab_snapshots(config_name)

    @work(thread=True)
    def _load_tab_snapshots(self, config_name: str) -> None:
        log.info("Loading snapshots for config '%s'", config_name)
        try:
            snaps = backend.get_snapshots(config_name)
        except backend.SudoExpiredError:
            log.warning("sudo expired while loading snapshots for '%s'", config_name)
            self.app.call_from_thread(self._show_sudo_expired)
            snaps = []
        except Exception:
            log.exception("Unexpected error loading snapshots for '%s'", config_name)
            snaps = []
        self.app.call_from_thread(self._populate_tab, config_name, snaps)

    def _populate_tab(self, config_name: str, snaps: list[backend.Snapshot]) -> None:
        self._loaded_configs.add(config_name)

        try:
            pane = self.query_one(f"#tab-{config_name}", TabPane)
        except Exception:
            return

        # Remove the placeholder spinner and label
        for widget_id in (f"spinner-{config_name}", f"loading-label-{config_name}"):
            try:
                pane.query_one(f"#{widget_id}").remove()
            except Exception:
                pass

        table = DataTable(id=f"table-{config_name}")
        status = Static("", classes="status-bar", id=f"status-{config_name}")
        pane.mount(table)
        pane.mount(status)

        table.cursor_type = "row"
        table.add_columns("#", "Type", "Date", "Description", "Used Space", "Cleanup", "RO")

        for snap in reversed(snaps):  # newest first
            if snap.number == 0:
                continue
            table.add_row(
                str(snap.number),
                snap.type,
                snap.date,
                snap.description[:50],
                snap.used_space or "-",
                snap.cleanup,
                "yes" if snap.read_only else "no",
                key=str(snap.number),
            )

        count = len([s for s in snaps if s.number != 0])
        self.query_one(f"#status-{config_name}", Static).update(f"{count} snapshots")

    def _show_sudo_expired(self) -> None:
        self._update_sudo_status()
        self.push_screen(SudoExpiredScreen())

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
        """Reload the active tab immediately; mark other tabs as unloaded (load on demand)."""
        cfg = self._get_active_config()
        if not cfg:
            return
        # Invalidate backend cache and clear app-level UI state
        backend.invalidate_cache()
        self._loaded_configs = set()
        # Rebuild each tab's placeholder spinner (drop existing table/status)
        for c in self.configs:
            try:
                pane = self.query_one(f"#tab-{c.name}", TabPane)
                pane.query("*").remove()
                pane.mount(BrailleSpinner(id=f"spinner-{c.name}"))
                pane.mount(Static("Loading snapshots…", id=f"loading-label-{c.name}",
                                  classes="status-bar"))
            except Exception:
                pass
        # Load the currently active tab right away
        self._load_tab_snapshots(cfg.name)

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
        try:
            ok, msg = backend.delete_snapshot(config_name, snap_num)
        except backend.SudoExpiredError:
            self.app.call_from_thread(self._show_sudo_expired)
            return
        self.app.call_from_thread(self._on_delete_done, ok, msg)

    def _on_delete_done(self, ok: bool, msg: str) -> None:
        self.notify(msg, severity="information" if ok else "error")
        if ok:
            self.action_refresh()

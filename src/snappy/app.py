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
    Tree,
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


_BAR_STEPS = " ▏▎▍▌▋▊▉█"  # 9 steps: index 0 = empty, index 8 = full block

def _make_bar(fraction: float, width: int = 8) -> str:
    """Return a Unicode block progress bar of `width` chars for fraction 0.0–1.0."""
    if fraction <= 0:
        return " " * width
    fraction = min(fraction, 1.0)
    total_eighths = round(fraction * width * 8)
    full = total_eighths // 8
    remainder = total_eighths % 8
    bar = "█" * full
    if remainder and full < width:
        bar += _BAR_STEPS[remainder]
        full += 1
    return bar.ljust(width)


# ── Braille Spinner ──────────────────────────────────────────────────────

class BrailleSpinner(Widget):
    """Animated braille spinner with an optional inline label."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    DEFAULT_CSS = """
    BrailleSpinner {
        width: auto;
        height: 1;
    }
    """

    def __init__(self, label: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._label = label

    def on_mount(self) -> None:
        self.auto_refresh = 1 / 10

    def render(self) -> Text:
        from time import time
        frame = self._FRAMES[int(time() * 10) % len(self._FRAMES)]
        text = Text()
        if self._label:
            text.append(self._label + " ", style="")
        text.append(frame, style="bold dark_orange")
        return text


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
        border: thick $secondary;
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
        border: thick $secondary;
        background: $surface;
        padding: 0 2 1 2;
    }
    #browse-tree {
        height: 1fr;
    }
    #browse-detail {
        height: auto;
        border-top: tall $accent;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        snapshot_path: Path,
        snapshot_label: str,
        config_name: str | None = None,
        snap_num: int | None = None,
    ) -> None:
        super().__init__()
        self.snapshot_path = snapshot_path
        self.snapshot_label = snapshot_label
        self._config_name = config_name
        self._snap_num = snap_num
        # Maps parent_path_str -> {child_path_str -> (tree_node, size_bytes)}
        self._level_sizes: dict[str, dict[str, tuple]] = {}
        self._sudo_expired_du_shown: bool = False
        # Path of the deepest expanded directory; its children show bright bars
        self._active_level_path: str | None = None
        # Absolute snapshot paths that differ from current filesystem; None = not yet loaded
        self._changed_paths: set[str] | None = None
        # All ancestor directories of changed paths (precomputed for O(1) dir lookup)
        self._dirty_dirs: set[str] = set()

    # Sentinel used as placeholder data to detect un-loaded directory nodes.
    _LOADING = object()

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            yield Label(f"Browsing: [bold]{self.snapshot_label}[/bold]")
            yield Tree("Loading…", id="browse-tree")
            yield Static("", id="browse-detail")

    def on_mount(self) -> None:
        self._load_dir_node(self.query_one("#browse-tree", Tree).root, self.snapshot_path)
        if self._config_name and self._snap_num is not None:
            self._fetch_snapshot_status(self._config_name, self._snap_num)

    @work(thread=True)
    def _load_dir_node(self, node, path: Path) -> None:
        try:
            entries = backend.browse_directory(path)
        except backend.SudoExpiredError:
            self.app.call_from_thread(
                lambda: node.set_label("sudo credentials expired")
            )
            return
        except Exception as e:
            self.app.call_from_thread(
                lambda: node.set_label(f"Error: {e}")
            )
            return
        self.app.call_from_thread(self._populate_node, node, path, entries)

    def _populate_node(
        self, node, path: Path, entries: list[backend.FileInfo]
    ) -> None:
        if node.is_root:
            node.set_label(str(path))

        path_str = str(path)
        level_map: dict[str, tuple] = {}

        for entry in entries:
            if entry.is_dir:
                child = node.add(Text(entry.name, style="bold"), data=entry)
                child.add("…", data=self._LOADING)
                level_map[entry.path] = (child, 0)
            else:
                leaf = node.add_leaf(Text(entry.name), data=entry)
                level_map[entry.path] = (leaf, entry.size)

        self._level_sizes[path_str] = level_map
        if self._active_level_path is None:
            self._active_level_path = path_str
        self._redraw_level_bars(path_str)

        for entry in entries:
            if entry.is_dir:
                child_node = level_map[entry.path][0]
                self._fetch_dir_size(path_str, entry.path, child_node)

        if node.is_root:
            node.expand()

    @work(thread=True)
    def _fetch_dir_size(self, parent_path_str: str, dir_path_str: str, node) -> None:
        try:
            size = backend.get_dir_size(dir_path_str)
        except backend.SudoExpiredError:
            self.app.call_from_thread(self._on_du_sudo_expired)
            return
        except Exception as e:
            log.warning("du failed for '%s': %s", dir_path_str, e)
            return
        self.app.call_from_thread(
            self._on_dir_size_ready, parent_path_str, dir_path_str, node, size
        )

    def _on_dir_size_ready(
        self, parent_path_str: str, dir_path_str: str, node, size: int
    ) -> None:
        level_map = self._level_sizes.get(parent_path_str)
        if level_map is None or dir_path_str not in level_map:
            return
        level_map[dir_path_str] = (node, size)
        self._redraw_level_bars(parent_path_str)

    def _redraw_all_levels(self) -> None:
        for path_str in list(self._level_sizes):
            self._redraw_level_bars(path_str)

    def _redraw_level_bars(self, parent_path_str: str) -> None:
        level_map = self._level_sizes.get(parent_path_str)
        if not level_map:
            return
        entries = [
            (node, size, node.data)
            for node, size in level_map.values()
            if isinstance(node.data, backend.FileInfo)
        ]
        if not entries:
            return
        max_size = max((size for _node, size, _entry in entries), default=0)
        max_name_len = max(len(entry.name) for _node, _size, entry in entries)
        active = (parent_path_str == self._active_level_path)
        bar_style = "dark_orange" if active else "dark_orange dim"
        for node, size, entry in entries:
            fraction = size / max_size if max_size > 0 else 0.0
            bar = _make_bar(fraction)
            size_str = _fmt_size(size) if size > 0 else "…"
            if self._changed_paths is None:
                # Status not yet loaded — no dimming
                is_changed = True
            elif entry.is_dir:
                is_changed = entry.path in self._dirty_dirs
            else:
                is_changed = entry.path in self._changed_paths
            label = Text()
            marker = "+" if (is_changed and not entry.is_dir) else " "
            if entry.is_dir:
                name_style = "bold" if is_changed else "bold dim"
            else:
                name_style = "" if is_changed else "dim"
            label.append(marker, style="dark_orange dim")
            label.append(entry.name.ljust(max_name_len), style=name_style)
            label.append("  ")
            label.append(bar, style=bar_style)
            label.append(f" {size_str}", style="dim")
            node.set_label(label)

    def _on_du_sudo_expired(self) -> None:
        if not self._sudo_expired_du_shown:
            self._sudo_expired_du_shown = True
            self.app.push_screen(SudoExpiredScreen())

    @work(thread=True)
    def _fetch_snapshot_status(self, config_name: str, snap_num: int) -> None:
        try:
            rel_paths = backend.get_snapshot_status(config_name, snap_num)
        except backend.SudoExpiredError:
            return
        except Exception as e:
            log.warning("snapper status failed: %s", e)
            return
        # Convert relative paths (e.g. "/etc/fstab") to absolute snapshot paths
        changed = {str(self.snapshot_path / p.lstrip("/")) for p in rel_paths}
        self.app.call_from_thread(self._on_status_ready, changed)

    def _on_status_ready(self, changed: set[str]) -> None:
        self._changed_paths = changed
        # Precompute all ancestor dirs of changed paths for O(1) directory lookup
        dirty: set[str] = set()
        for p in changed:
            parent = Path(p).parent
            while str(parent) not in dirty:
                dirty.add(str(parent))
                if parent == parent.parent:
                    break
                parent = parent.parent
        self._dirty_dirs = dirty
        self._redraw_all_levels()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if not isinstance(node.data, backend.FileInfo) or not node.data.is_dir:
            return
        # Update active level before collapsing siblings so their collapse
        # events don't incorrectly revert _active_level_path
        self._active_level_path = str(node.data.path)
        self._redraw_all_levels()
        # Collapse all siblings — only one branch open at a time
        if node.parent is not None:
            for sibling in list(node.parent.children):
                if sibling is not node and sibling.is_expanded:
                    sibling.collapse()
        children = list(node.children)
        if len(children) == 1 and children[0].data is self._LOADING:
            node.remove_children()
            self._load_dir_node(node, Path(node.data.path))

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        node = event.node
        if not isinstance(node.data, backend.FileInfo) or not node.data.is_dir:
            return
        # If the active level was under this node, revert to its parent level
        if self._active_level_path == str(node.data.path):
            self._active_level_path = str(Path(node.data.path).parent)
            self._redraw_all_levels()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if isinstance(event.node.data, backend.FileInfo):
            self._show_file_detail(event.node.data)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, backend.FileInfo):
            self._show_file_detail(event.node.data)

    def _show_file_detail(self, entry: backend.FileInfo) -> None:
        detail = self.query_one("#browse-detail", Static)
        size = entry.size
        if entry.is_dir:
            cached = backend.get_cached_dir_size(entry.path)
            if cached is not None:
                size = cached

        parent_path_str = str(Path(entry.path).parent)
        size_display = _fmt_size(size) if size > 0 else ("…" if entry.is_dir else "-")
        kind = "Directory" if entry.is_dir else "File"

        # Fixed-width columns so rows align; C1 fits "modified: 2024-01-15 10:30"
        C1, C2, SEP = 28, 18, "  [dim]│[/dim]  "
        def col(label: str, value: str, width: int) -> str:
            pad = " " * max(0, width - len(label) - 2 - len(value))
            return f"[bold dim]{label}:[/bold dim] {value}{pad}"

        line1 = (
            col("name", entry.name[:20], C1) + SEP +
            col("type", kind, C2) + SEP +
            f"[bold dim]size:[/bold dim] {size_display}"
        )
        if self._changed_paths is None:
            status_text = "[dim]checking…[/dim]"
        elif entry.is_dir:
            if entry.path in self._dirty_dirs:
                status_text = "[yellow]has changes[/yellow]"
            else:
                status_text = "[dim]all same as disk[/dim]"
        else:
            if entry.path in self._changed_paths:
                status_text = "[yellow]stored in snapshot[/yellow]"
            else:
                status_text = "[dim]same as disk[/dim]"

        line2 = (
            col("modified", _fmt_mtime(entry.mtime), C1) + SEP +
            col("perms", entry.permissions, C2) + SEP +
            f"[bold dim]status:[/bold dim] {status_text}"
        )
        detail.update(f"{line1}\n{line2}")


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
        color: $accent;
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
        self._desc_column_keys: dict[str, object] = {}  # config_name -> description ColumnKey

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
            self.set_interval(10, self._update_sudo_status)

    @work(thread=True)
    def _update_sudo_status(self) -> None:
        """Update the sudo countdown; keep credentials alive when close to expiry."""
        remaining = backend.sudo_seconds_remaining()
        # Keep-alive: if 30s or less remain, attempt a silent refresh via check_sudo().
        # On success, _sudo_last_confirmed resets and remaining jumps back to ~5m.
        # On failure, remaining stays at 0 and we show the expiry warning.
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
        elif remaining < 60:
            widget.update(f"sudo: {remaining}s remaining")
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
                pane.mount(BrailleSpinner("Loading snapshots…", id=f"spinner-{cfg.name}",
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

        # Remove the placeholder spinner
        for widget_id in (f"spinner-{config_name}",):
            try:
                pane.query_one(f"#{widget_id}").remove()
            except Exception:
                pass

        table = DataTable(id=f"table-{config_name}")
        status = Static("", classes="status-bar", id=f"status-{config_name}")
        pane.mount(table)
        pane.mount(status)

        table.cursor_type = "row"
        col_keys = table.add_columns("#", "Type", "Date ↓", "Description", "Used Space", "Cleanup", "RO")
        self._desc_column_keys[config_name] = col_keys[3]

        for snap in reversed(snaps):  # newest first
            if snap.number == 0:
                continue
            table.add_row(
                str(snap.number),
                snap.type,
                snap.date,
                snap.description[:50],
                _fmt_size(int(snap.used_space)) if snap.used_space else "-",
                snap.cleanup,
                "yes" if snap.read_only else "no",
                key=str(snap.number),
            )

        count = len([s for s in snaps if s.number != 0])
        self.query_one(f"#status-{config_name}", Static).update(f"{count} snapshots")
        self.call_after_refresh(self._fit_description_column, table, config_name)

    def _fit_description_column(self, table: DataTable, config_name: str) -> None:
        """Expand the Description column to fill available horizontal space."""
        desc_key = self._desc_column_keys.get(config_name)
        if desc_key is None or desc_key not in table.columns:
            return
        total_width = table.size.width
        if total_width == 0:
            return
        desc_col = table.columns[desc_key]
        other_width = sum(
            col.get_render_width(table)
            for key, col in table.columns.items()
            if key != desc_key
        )
        available = total_width - other_width - 2 * table.cell_padding
        if available < 1:
            return
        desc_col.auto_width = False
        desc_col.width = available
        table._require_update_dimensions = True
        table.refresh()

    def on_resize(self) -> None:
        """Re-fit the Description column for all loaded tables when the terminal is resized."""
        self.call_after_refresh(self._fit_all_description_columns)

    def _fit_all_description_columns(self) -> None:
        for config_name in self._loaded_configs:
            try:
                table = self.query_one(f"#table-{config_name}", DataTable)
            except Exception:
                continue
            self._fit_description_column(table, config_name)

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
                pane.mount(BrailleSpinner("Loading snapshots…", id=f"spinner-{c.name}",
                                         classes="status-bar"))
            except Exception:
                pass
        # Load the currently active tab right away
        self._load_tab_snapshots(cfg.name)

    def action_file_search(self) -> None:
        cfg = self._get_active_config()
        if cfg:
            self.push_screen(FileSearchScreen(cfg))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Only act on the main snapshot tables, not the file-search results table
        if event.data_table.id and event.data_table.id.startswith("table-"):
            self.action_browse()

    def action_browse(self) -> None:
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if cfg and snap_num:
            snap_path = backend.get_snapshot_path(cfg, snap_num)
            snaps = backend.get_snapshots(cfg.name)
            snap = next((s for s in snaps if s.number == snap_num), None)
            size_str = _fmt_size(int(snap.used_space)) if snap and snap.used_space else ""
            label = f"Config: {cfg.name}  Snapshot: #{snap_num}"
            if size_str:
                label += f"  ({size_str})"
            self.push_screen(BrowseScreen(snap_path, label, cfg.name, snap_num))

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

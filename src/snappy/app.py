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
    s = humanize.naturalsize(size_bytes, binary=True)
    return s.replace(" Bytes", " B").replace(" Byte", " B")


def _fmt_size_styled(size_bytes: int) -> tuple[str, str]:
    """Return (text, rich_style) for tree node size display.

    Uses >5.1f for KiB+ so decimal points align within a level when rjust'd.
    Color encodes magnitude: B=dim, KiB=normal, MiB=dim orange3, GiB+=bold orange1.
    """
    if size_bytes <= 0:
        return "-", "dim"
    if size_bytes < 1024:
        return f"{size_bytes:>5} B  ", "dim"  # pad to same width as ">5.1f KiB"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:>5.1f} KiB", ""
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:>5.1f} MiB", "dim orange3"
    elif size_bytes < 1024 ** 4:
        return f"{size_bytes / 1024 ** 3:>5.1f} GiB", "bold orange1"
    else:
        return f"{size_bytes / 1024 ** 4:>5.1f} TiB", "bold orange1"


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
        color: $text-disabled;
        margin-bottom: 1;
        height: auto;
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
                "Matches any part of the path across all snapshots."
                "\nWildcards: [$accent]*[/] any string  [$accent]?[/] one character",
                id="search-hint",
            )
            yield Input(placeholder="search term (e.g. fstab, home/user, .conf)", id="search-input")
            yield BrailleSpinner(id="search-loading")
            yield DataTable(id="search-results")

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.add_columns("Snapshot #", "Date", "Path", "Size", "Modified")
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        pattern = event.value.strip()
        if not pattern:
            return
        self.query_one("#search-loading", BrailleSpinner).styles.display = "block"
        self._do_search(pattern)

    @work(thread=True)
    def _do_search(self, pattern: str) -> None:
        try:
            snapshots = backend.get_snapshots(self.config.name)  # cache hit
            results = backend.search_files_in_snapshots(self.config, pattern, snapshots)
        except backend.SudoExpiredError:
            self.app.call_from_thread(self._on_sudo_expired_search)
            return
        self.app.call_from_thread(self._populate_results, results)

    def _on_sudo_expired_search(self) -> None:
        self.query_one("#search-loading", BrailleSpinner).styles.display = "none"
        self.app.push_screen(SudoExpiredScreen())

    def _populate_results(self, results: list[backend.FileSearchMatch]) -> None:
        self.query_one("#search-loading", BrailleSpinner).styles.display = "none"
        table = self.query_one("#search-results", DataTable)
        table.clear()
        for r in results:
            table.add_row(
                str(r.snapshot_number),
                r.snapshot_date,
                r.path,
                _fmt_size(r.size),
                _fmt_mtime(r.mtime),
            )


# ── Snapshot Cost Screen ─────────────────────────────────────────────────

class SnapshotCostScreen(ModalScreen):
    """Show files exclusive to a snapshot — its incremental disk cost."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("s", "toggle_sort", "Sort"),
    ]

    CSS = """
    SnapshotCostScreen {
        align: center middle;
    }
    #cost-dialog {
        width: 90%;
        height: 85%;
        border: thick $secondary;
        background: $surface;
        padding: 1 2;
    }
    #cost-hint {
        color: $text-disabled;
        margin-bottom: 1;
        height: auto;
    }
    #cost-results {
        height: 1fr;
    }
    #cost-loading {
        height: 1;
        display: none;
    }
    #cost-summary {
        height: auto;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        config: backend.SnapperConfig,
        snap_num: int,
        next_snap_num: int,
        snap_date: str,
        next_label: str,
    ) -> None:
        super().__init__()
        self.config = config
        self.snap_num = snap_num
        self.next_snap_num = next_snap_num
        self.snap_date = snap_date
        self.next_label = next_label
        self._files: list[backend.ExclusiveFile] = []
        self._sort_by_size: bool = True

    def compose(self) -> ComposeResult:
        with Vertical(id="cost-dialog"):
            yield Label(
                f"Exclusive files in snapshot [bold]#{self.snap_num}[/bold] vs {self.next_label}"
            )
            yield Label(
                f"Files present in #{self.snap_num} ({self.snap_date}) but absent from {self.next_label}.",
                id="cost-hint",
            )
            yield BrailleSpinner("Comparing snapshots…", id="cost-loading")
            yield DataTable(id="cost-results")
            yield Static("", id="cost-summary")

    def on_mount(self) -> None:
        table = self.query_one("#cost-results", DataTable)
        table.add_columns("Size", "Path")
        self.query_one("#cost-loading", BrailleSpinner).styles.display = "block"
        self._compute()

    @work(thread=True)
    def _compute(self) -> None:
        try:
            files = backend.get_snapshot_exclusive_files(
                self.config, self.config.name, self.snap_num, self.next_snap_num,
            )
        except backend.SudoExpiredError:
            self.app.call_from_thread(self._on_sudo_expired)
            return
        self.app.call_from_thread(self._populate, files)

    def _on_sudo_expired(self) -> None:
        self.query_one("#cost-loading", BrailleSpinner).styles.display = "none"
        self.app.push_screen(SudoExpiredScreen())

    def _populate(self, files: list[backend.ExclusiveFile]) -> None:
        self.query_one("#cost-loading", BrailleSpinner).styles.display = "none"
        self._files = files
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#cost-results", DataTable)
        table.clear()
        total = sum(f.size for f in self._files)
        if self._sort_by_size:
            sorted_files = sorted(self._files, key=lambda f: -f.size)
            for f in sorted_files:
                table.add_row(_fmt_size(f.size), f.path)
            sort_label = "by size"
        else:
            # Group files by immediate parent directory.
            from collections import defaultdict
            dir_files: dict[str, list[backend.ExclusiveFile]] = defaultdict(list)
            for f in self._files:
                dir_files[str(Path(f.path).parent)].append(f)
            # Compute recursive directory subtotals (each ancestor gets the sum).
            dir_sizes: dict[str, int] = defaultdict(int)
            for f in self._files:
                p = Path(f.path).parent
                while str(p) != p.root and str(p) != ".":
                    dir_sizes[str(p)] += f.size
                    p = p.parent
            # Show all directories with subtotals (parents before children).
            for dir_path in sorted(dir_sizes):
                table.add_row(
                    Text(f"[{_fmt_size(dir_sizes[dir_path])}]", style="bold cyan"),
                    Text(f"{dir_path}/", style="bold cyan"),
                )
                direct = dir_files.get(dir_path, [])
                if len(direct) > 1 and dir_sizes[dir_path] >= 1024 * 1024:
                    for f in sorted(direct, key=lambda f: f.path):
                        table.add_row(_fmt_size(f.size), f"  {Path(f.path).name}")
            sort_label = "by path"
        self.query_one("#cost-summary", Static).update(
            f"{len(self._files)} exclusive files — total: [bold]{_fmt_size(total)}[/bold]  (sorted {sort_label}, [bold]s[/bold] to toggle)"
        )

    def action_toggle_sort(self) -> None:
        if not self._files:
            return
        self._sort_by_size = not self._sort_by_size
        self._refresh_table()


# ── Browse Snapshot Screen ───────────────────────────────────────────────

class BrowseScreen(ModalScreen):
    """Browse a snapshot's directory tree."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("s", "toggle_sort", "Sort"),
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
    #browse-header {
        height: 1;
        layout: horizontal;
    }
    #browse-status-spinner {
        width: auto;
        margin-left: 2;
        display: none;
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
        # Maps parent_path_str -> parent TreeNode (for sorting)
        self._level_nodes: dict[str, object] = {}
        self._sudo_expired_du_shown: bool = False
        # Path of the deepest expanded directory; its children show bright bars
        self._active_level_path: str | None = None
        # Maps absolute snapshot path -> snapper status char ('-', 'c', etc.); None = not yet loaded
        self._file_statuses: dict[str, str] | None = None
        # All ancestor directories of paths with any status entry (for O(1) dir lookup)
        self._dirty_dirs: set[str] = set()
        # Sort order: False = alphabetical (dirs first), True = descending size
        self._sort_by_size: bool = False

    # Sentinel used as placeholder data to detect un-loaded directory nodes.
    _LOADING = object()

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            with Horizontal(id="browse-header"):
                yield Label(f"Browsing: [bold]{self.snapshot_label}[/bold]")
                yield BrailleSpinner("checking status…", id="browse-status-spinner")
            yield Tree("Loading…", id="browse-tree")
            yield Static("", id="browse-detail")

    def on_mount(self) -> None:
        self._load_dir_node(self.query_one("#browse-tree", Tree).root, self.snapshot_path)
        if self._config_name and self._snap_num is not None:
            self.query_one("#browse-status-spinner", BrailleSpinner).styles.display = "block"
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
        self._level_nodes[path_str] = node
        if self._active_level_path is None:
            self._active_level_path = path_str
        if self._sort_by_size:
            self._sort_level(path_str)
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
        if self._sort_by_size:
            self._sort_level(parent_path_str)
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
        _NAME_CAP = 40
        max_name_len = min(max(len(entry.name) for _node, _size, entry in entries), _NAME_CAP)
        active = (parent_path_str == self._active_level_path)
        bar_style = "dark_orange" if active else "dark_orange dim"
        # Pre-compute styled sizes so we can rjust all to the same width,
        # which aligns decimal points across siblings in the same unit.
        styled_sizes = []
        for _node, size, entry in entries:
            if size > 0:
                styled_sizes.append(_fmt_size_styled(size))
            else:
                styled_sizes.append(("…" if entry.is_dir else "-", "dim"))
        max_sz_w = max(len(t) for t, _ in styled_sizes)

        for i, (node, size, entry) in enumerate(entries):
            fraction = size / max_size if max_size > 0 else 0.0
            bar = _make_bar(fraction)
            size_str, size_style = styled_sizes[i]
            if entry.is_dir:
                marker = " "
                is_changed = entry.path in self._dirty_dirs
            else:
                if self._file_statuses is None:
                    marker, is_changed = " ", False
                else:
                    status_char = self._file_statuses.get(entry.path)
                    if status_char == "-":
                        marker, is_changed = "+", True   # in snapshot, not on disk
                    elif status_char is not None:
                        marker, is_changed = "~", True   # exists on disk but differs
                    else:
                        marker, is_changed = " ", False  # same as disk
            label = Text()
            if entry.is_dir:
                name_style = "bold" if is_changed else "bold dim"
            else:
                name_style = "" if is_changed else "dim"
            # Files lack the expand/collapse triangle that folders have, so add 2 spaces for alignment
            marker_prefix = "" if entry.is_dir else "  "
            label.append(marker_prefix + marker, style="dark_orange dim")
            name = entry.name
            if len(name) > max_name_len:
                name = name[:max_name_len - 1] + "…"
            label.append(name.ljust(max_name_len), style=name_style)
            label.append("  ")
            label.append(bar, style=bar_style)
            label.append(" " + size_str.rjust(max_sz_w), style=size_style)
            node.set_label(label)

    def _on_du_sudo_expired(self) -> None:
        if not self._sudo_expired_du_shown:
            self._sudo_expired_du_shown = True
            self.app.push_screen(SudoExpiredScreen())

    def _resolve_status_path(self, rel_path: str) -> str:
        """Convert a snapper status path (relative to subvolume) to absolute snapshot path."""
        snapshot_parts = self.snapshot_path.parts
        try:
            snapshots_idx = snapshot_parts.index(".snapshots")
            if snapshots_idx > 0:
                subvolume = Path(*snapshot_parts[:snapshots_idx])
                path_obj = Path(rel_path)
                try:
                    relative_p = path_obj.relative_to(subvolume)
                    return str(self.snapshot_path / str(relative_p).lstrip("/"))
                except ValueError:
                    return str(self.snapshot_path / rel_path.lstrip("/"))
            else:
                return str(self.snapshot_path / rel_path.lstrip("/"))
        except (ValueError, IndexError):
            return str(self.snapshot_path / rel_path.lstrip("/"))

    @work(thread=True)
    def _fetch_snapshot_status(self, config_name: str, snap_num: int) -> None:
        try:
            rel_statuses = backend.get_snapshot_status(config_name, snap_num)
        except backend.SudoExpiredError:
            return
        except Exception as e:
            log.warning("snapper status failed: %s", e)
            return
        statuses = {
            self._resolve_status_path(p): s for p, s in rel_statuses.items()
        }
        self.app.call_from_thread(self._on_status_ready, statuses)

    def _on_status_ready(self, statuses: dict[str, str]) -> None:
        """Apply all status results and finalize."""
        self._file_statuses = statuses
        dirty: set[str] = set()
        for p in statuses:
            parent = Path(p).parent
            while str(parent) not in dirty:
                dirty.add(str(parent))
                if parent == parent.parent:
                    break
                parent = parent.parent
        self._dirty_dirs = dirty
        self._on_status_stream_complete()

    def _on_status_stream_complete(self) -> None:
        self.query_one("#browse-status-spinner", BrailleSpinner).styles.display = "none"
        self._redraw_all_levels()

    def action_toggle_sort(self) -> None:
        self._sort_by_size = not self._sort_by_size
        for path_str in list(self._level_nodes):
            self._sort_level(path_str)

    def _sort_level(self, path_str: str) -> None:
        parent_node = self._level_nodes.get(path_str)
        if parent_node is None:
            return
        level_map = self._level_sizes.get(path_str, {})
        if self._sort_by_size:
            parent_node._children.sort(
                key=lambda n: (
                    -level_map.get(n.data.path, (None, 0))[1]
                    if isinstance(n.data, backend.FileInfo) else 0
                )
            )
        else:
            parent_node._children.sort(
                key=lambda n: (not n.data.is_dir, n.data.name.lower())
                if isinstance(n.data, backend.FileInfo) else (True, "")
            )
        self.query_one("#browse-tree", Tree)._invalidate()

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
        if self._file_statuses is None:
            status_text = "[dim]checking…[/dim]"
        elif entry.is_dir:
            if entry.path in self._dirty_dirs:
                status_text = "[yellow]has changes[/yellow]"
            else:
                status_text = "[dim]all same as disk[/dim]"
        else:
            status_char = self._file_statuses.get(entry.path)
            if status_char == "-":
                status_text = "[yellow]only in snapshot[/yellow]"
            elif status_char is not None:
                status_text = "[yellow]differs from disk[/yellow]"
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
        Binding("e", "snapshot_cost", "Exclusive Size"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.configs: list[backend.SnapperConfig] = []
        self.fs_usage: backend.FilesystemUsage | None = None
        self._loaded_configs: set[str] = set()  # configs whose snapshots have been fetched
        self._desc_column_keys: dict[str, object] = {}  # config_name -> description ColumnKey
        self._pane_to_config: dict[str, str] = {}  # pane_id -> config_name (for tracking tabs)

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

    def action_quit(self) -> None:
        backend.kill_running_subprocesses()
        self.exit()

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
            pane_id = cfg.name
            pane = TabPane(f"{cfg.name} ({cfg.subvolume})", id=pane_id)
            tabs.add_pane(pane)
            # Track the mapping from pane ID to config name
            self._pane_to_config[pane_id] = cfg.name

        # Mount per-tab spinners after panes are added, then load the active tab
        self.set_timer(0.1, self._init_tab_spinners)

    def _init_tab_spinners(self) -> None:
        """Add a spinner to every tab pane, then kick off loading for the active tab."""
        for cfg in self.configs:
            try:
                pane = self.query_one(f"#{cfg.name}", TabPane)
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
        if not event.tab or not event.tab.id:
            return

        # Try to find config using the pane mapping first (most reliable)
        config_name = self._pane_to_config.get(event.tab.id)

        if not config_name:
            # Fallback: try to extract from tab ID (handles malformed IDs)
            raw_id = event.tab.id
            # Remove "--content-" prefix if present
            if raw_id.startswith("--content-"):
                raw_id = raw_id[len("--content-"):]
            # Remove "tab-" prefix if present
            if raw_id.startswith("tab-"):
                config_name = raw_id[len("tab-"):]
            else:
                # Shouldn't happen, but use as-is if no known prefix
                config_name = raw_id
            log.warning("Tab ID '%s' not in pane mapping; extracted config_name='%s'", event.tab.id, config_name)

        if config_name and config_name not in self._loaded_configs:
            self._load_tab_snapshots(config_name)

    @work(thread=True)
    def _load_tab_snapshots(self, config_name: str) -> None:
        if config_name in self._loaded_configs:
            log.debug("Skipping duplicate load for already-loaded config '%s'", config_name)
            return
        log.info("Loading snapshots for config '%s'", config_name)
        snaps = []
        error_msg = None

        # Check if config_name is valid (not malformed like "tab-home")
        if config_name.startswith("tab-") or config_name.startswith("--content-"):
            error_msg = f"Invalid config name: '{config_name}' (likely a tab ID, not a config name)"
            log.error(error_msg)
        else:
            # Check if this config is actually known
            known_configs = {cfg.name for cfg in self.configs}
            if config_name not in known_configs:
                error_msg = f"Unknown config: '{config_name}'"
                log.error(error_msg)
            else:
                try:
                    snaps = backend.get_snapshots(config_name)
                except backend.SudoExpiredError:
                    log.warning("sudo expired while loading snapshots for '%s'", config_name)
                    self.app.call_from_thread(self._show_sudo_expired)
                    error_msg = "sudo credentials expired"
                except Exception as e:
                    log.exception("Unexpected error loading snapshots for '%s'", config_name)
                    error_msg = f"Failed to load snapshots: {type(e).__name__}"

        self.app.call_from_thread(self._populate_tab, config_name, snaps, error_msg)

    def _populate_tab(self, config_name: str, snaps: list[backend.Snapshot], error_msg: str | None = None) -> None:
        self._loaded_configs.add(config_name)

        try:
            pane = self.query_one(f"#{config_name}", TabPane)
        except Exception:
            return

        # Clear all existing widgets from the pane (spinner, old tables, etc.)
        pane.query("*").remove()

        # If there was an error loading snapshots, display the error message
        if error_msg:
            error_widget = Static(f"[red]✗ {error_msg}[/red]", classes="status-bar")
            pane.mount(error_widget)
            return

        # Remove any existing table/status widgets by ID to avoid DuplicateIds errors
        try:
            pane.query_one(f"#table-{config_name}").remove()
        except:
            pass
        try:
            pane.query_one(f"#status-{config_name}").remove()
        except:
            pass

        table = DataTable(id=f"table-{config_name}")
        status = Static("", classes="status-bar", id=f"status-{config_name}")
        try:
            pane.mount(table)
        except Exception as e:
            log.warning("Failed to mount table for config '%s': %s (trying to remove and retry)", config_name, e)
            try:
                pane.query_one(f"#table-{config_name}").remove()
                pane.mount(table)
            except Exception as e2:
                log.error("Could not mount table after retry: %s", e2)
                return
        try:
            pane.mount(status)
        except Exception as e:
            log.warning("Failed to mount status widget for config '%s': %s", config_name, e)

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

        # Try to find config using the pane mapping first (most reliable)
        config_name = self._pane_to_config.get(active_id)

        if not config_name:
            # Fallback: try to extract from active_id (handles malformed IDs)
            raw_id = active_id
            # Remove "--content-" prefix if present
            if raw_id.startswith("--content-"):
                raw_id = raw_id[len("--content-"):]
            # Remove "tab-" prefix if present
            if raw_id.startswith("tab-"):
                config_name = raw_id[len("tab-"):]
            else:
                # Shouldn't happen, but use as-is if no known prefix
                config_name = raw_id

        # Find the matching config
        for cfg in self.configs:
            if cfg.name == config_name:
                return cfg
        return self.configs[0] if self.configs else None

    def _get_selected_snapshot_number(self) -> int | None:
        table: DataTable | None = None
        # Prefer the focused DataTable (avoids issues with duplicate widget IDs)
        focused = self.screen.focused
        if isinstance(focused, DataTable) and focused.id and focused.id.startswith("table-"):
            table = focused
        else:
            # Fallback: query by active config name
            cfg = self._get_active_config()
            if not cfg:
                return None
            try:
                table = self.query_one(f"#table-{cfg.name}", DataTable)
            except Exception:
                return None
        if table is not None and table.row_count > 0:
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
                pane = self.query_one(f"#{c.name}", TabPane)
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
        if not event.data_table.id or not event.data_table.id.startswith("table-"):
            return

        # Extract config name from the table ID (e.g., "table-root" -> "root")
        config_name = event.data_table.id[len("table-"):]
        snap_num = self._get_selected_snapshot_number_for_config(config_name, event.data_table)

        if snap_num:
            cfg = next((c for c in self.configs if c.name == config_name), None)
            if cfg:
                self._browse_snapshot(cfg, snap_num)
                return

        log.warning("on_data_table_row_selected: could not browse snapshot from table %s", event.data_table.id)

    def _get_selected_snapshot_number_for_config(self, config_name: str, table: DataTable) -> int | None:
        """Get the selected snapshot number from a specific table."""
        if table.cursor_row is not None and table.row_count > 0:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            snap_num = int(row_key.value)
            log.debug("_get_selected_snapshot_number_for_config: config=%s, snap_num=%s", config_name, snap_num)
            return snap_num
        log.warning("_get_selected_snapshot_number_for_config: cursor_row=%s, row_count=%s for config %s", table.cursor_row, table.row_count, config_name)
        return None

    def _browse_snapshot(self, cfg: backend.SnapperConfig, snap_num: int) -> None:
        """Open the browse screen for a given config and snapshot."""
        snap_path = backend.get_snapshot_path(cfg, snap_num)
        snaps = backend.get_snapshots(cfg.name)
        snap = next((s for s in snaps if s.number == snap_num), None)
        size_str = _fmt_size(int(snap.used_space)) if snap and snap.used_space else ""
        label = f"Config: {cfg.name}  Snapshot: #{snap_num}"
        if size_str:
            label += f"  ({size_str})"
        self.push_screen(BrowseScreen(snap_path, label, cfg.name, snap_num))

    def action_browse(self) -> None:
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if cfg and snap_num:
            self._browse_snapshot(cfg, snap_num)

    def action_delete_snapshot(self) -> None:
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if cfg and snap_num:
            self.push_screen(
                ConfirmDeleteScreen(cfg.name, snap_num),
                callback=self._handle_delete,
            )

    def action_snapshot_cost(self) -> None:
        cfg = self._get_active_config()
        snap_num = self._get_selected_snapshot_number()
        if not cfg or not snap_num:
            return
        snaps = backend.get_snapshots(cfg.name)
        sorted_snaps = sorted(
            [s for s in snaps if s.number > 0], key=lambda s: s.number,
        )
        current_idx = next(
            (i for i, s in enumerate(sorted_snaps) if s.number == snap_num), None,
        )
        if current_idx is None:
            return
        current_snap = sorted_snaps[current_idx]
        if current_idx + 1 < len(sorted_snaps):
            next_snap = sorted_snaps[current_idx + 1]
            next_num = next_snap.number
            next_label = f"#{next_snap.number} ({next_snap.date})"
        else:
            next_num = 0
            next_label = "current filesystem"
        self.push_screen(SnapshotCostScreen(
            cfg, snap_num, next_num, current_snap.date, next_label,
        ))

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

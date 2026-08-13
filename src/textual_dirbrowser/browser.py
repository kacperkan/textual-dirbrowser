"""Textual directory browser with injectable listing backends.

The browser has no storage knowledge of its own: every mode (local FS, S3,
remote pod, ...) is a caller-supplied ``list_fn``/``parent_fn`` pair. The one
exception is destination mode, which is local-filesystem by design (it counts
existing entries and creates directories with ``pathlib``).
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape as markup_escape
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Rule, Static

from textual_dirbrowser.css import MODAL_CSS, TUI_CSS

# Number of sample files shown in the preview pane.
PREVIEW_SAMPLE = 15

# Max rows mounted into the ListView at once. ListView is not virtualised, so
# every entry becomes a live widget — mounting thousands of them freezes the UI
# for seconds on each rebuild. Filtering still runs over the full listing; we
# only cap how many matches are rendered, and surface the overflow as a hint.
RENDER_CAP = 500

# Debounce window (seconds) for live filtering, so rapid typing coalesces into
# a single rebuild instead of one per keystroke.
FILTER_DEBOUNCE = 0.08

# Sort modes cycled by `o`, in order, with their header labels.
SORT_MODES = ("mtime_desc", "mtime_asc", "name_asc", "name_desc")
SORT_LABELS = {
    "mtime_desc": "mtime↓",
    "mtime_asc": "mtime↑",
    "name_asc": "name↑",
    "name_desc": "name↓",
}


def _entry_name(value: str) -> str:
    """Basename of a browser entry value (handles trailing slash and s3://)."""
    return value.rstrip("/").rsplit("/", 1)[-1]


def _apply_sort(entries: list[DirEntry], mode: str) -> list[DirEntry]:
    """Sort entries. list_fn returns them mtime-newest-first, so mtime_desc is
    the identity and mtime_asc is the reverse; name modes sort by basename."""
    if mode == "mtime_desc":
        return list(entries)
    if mode == "mtime_asc":
        return list(reversed(entries))
    rev = mode == "name_desc"
    return sorted(entries, key=lambda e: _entry_name(e.value).lower(), reverse=rev)


def _text_matches(text: str, flt: str) -> bool:
    """Case-insensitive match: plain text is a substring, a pattern containing
    glob metacharacters is matched with fnmatch."""
    low = flt.lower()
    text = text.lower()
    if any(c in flt for c in "*?["):
        return fnmatch.fnmatch(text, low)
    return low in text


def _apply_filter(entries: list[DirEntry], flt: str) -> list[DirEntry]:
    """Filter entries by basename. Plain text is a case-insensitive substring;
    a value containing glob metacharacters is matched with fnmatch."""
    if not flt:
        return list(entries)
    return [e for e in entries if _text_matches(_entry_name(e.value), flt)]


def human_size(num: float) -> str:
    """Format a byte count as a short human-readable string (e.g. 4.2 GB)."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


@dataclass
class DirEntry:
    label: str
    value: str
    # False marks a *leaf* entry (e.g. a file): it can be Space-selected but is
    # never navigated into (→/l is a no-op) and can't be a `d` output mark.
    # Defaults True so every existing caller keeps directory behaviour.
    is_dir: bool = True


@dataclass
class PreviewInfo:
    """Recursive listing summary for the highlighted directory."""

    count: int = 0
    total_bytes: int = 0
    # (relative path, size in bytes) for the first PREVIEW_SAMPLE files.
    sample: list[tuple[str, int]] = field(default_factory=list)
    # True when the listing was capped (totals are a lower bound).
    partial: bool = False


def count_existing(path: str) -> int:
    """Number of entries already inside a local directory (0 if absent)."""
    try:
        p = Path(path)
        if not p.is_dir():
            return 0
        return sum(1 for _ in p.iterdir())
    except OSError:
        return 0


class _NewDirModal(ModalScreen[str | None]):
    DEFAULT_CSS = MODAL_CSS + """
    _NewDirModal {
        align: center middle;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("New directory name")
            yield Input(placeholder="dirname", id="dir-input")

    def on_mount(self) -> None:
        self.query_one("#dir-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.stop()


class _GoToModal(ModalScreen[str | None]):
    DEFAULT_CSS = MODAL_CSS + """
    _GoToModal {
        align: center middle;
    }
    """

    def __init__(self, prefill: str) -> None:
        super().__init__()
        self._prefill = prefill

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Go to path")
            yield Input(value=self._prefill, id="goto-input")

    def on_mount(self) -> None:
        inp = self.query_one("#goto-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.stop()


class BrowserApp(App[list[str]]):
    DEFAULT_CSS = TUI_CSS + """
    #path-bar {
        height: auto;
        border-bottom: solid $primary;
    }

    #main {
        height: 1fr;
        margin: 0 1 1 1;
        border: round $primary;
    }

    #browser-panel {
        width: 2fr;
        border-right: solid $primary;
    }

    #nav {
        height: 1fr;
    }

    #filter-bar {
        height: auto;
        display: none;
        background: $panel;
        color: $accent;
        padding: 0 1;
        border-bottom: solid $accent;
    }

    #filter-bar.editing {
        display: block;
    }

    #selected-panel {
        width: 1fr;
        padding: 0 2;
        background: $panel;
    }

    #preview-title,
    #selected-title {
        height: 1;
        padding: 1 0 0 0;
        text-style: bold;
        color: $accent;
    }

    #preview-divider,
    #selected-divider {
        color: $accent;
        margin: 0;
        height: 1;
    }

    #preview-body {
        height: auto;
        max-height: 60%;
        padding: 1 0;
        color: $foreground;
    }

    #selected-items {
        padding: 1 0;
        color: $foreground;
    }
    """

    # All bindings are priority so they win over the focused ListView's own
    # keys, which otherwise shadow them: ListView binds enter→select_cursor and
    # (via its scroll-container base) left/right→horizontal scroll. Without
    # priority the widget eats those before the app sees them — the same reason
    # MultiSelectApp marks its bindings priority. While the filter draft is
    # being edited, check_action() disables every binding so these keys fall
    # through to on_key() and type into the filter instead.
    BINDINGS = [
        Binding("enter", "quit_done", "Confirm", priority=True),
        Binding("q", "abort", "Quit", priority=True),
        Binding("escape", "abort", "Cancel", priority=True),
        Binding("left", "go_up", "← Back", priority=True),
        Binding("right", "enter_dir", "→ Enter", priority=True),
        Binding("space", "toggle_select", "Select", priority=True),
        Binding("a", "add_current", "Add dir", priority=True),
        Binding("n", "new_dir", "New dir", priority=True),
        Binding("slash", "filter", "Filter", priority=True),
        Binding("o", "cycle_sort", "Sort", priority=True),
        Binding("colon", "goto", "Go to", priority=True),
        Binding("g", "vim_top", "Top", show=False, priority=True),
        Binding("G", "vim_bottom", "Bottom", show=False, priority=True),
    ]

    def __init__(
        self,
        title_prefix: str,
        initial: str,
        root: str,
        list_fn: Callable[[str], tuple[list[DirEntry], int]],
        parent_fn: Callable[[str], str],
        pick_current: bool = False,
        preview_fn: Callable[[str], PreviewInfo] | None = None,
    ) -> None:
        super().__init__()
        self._title_prefix = title_prefix
        self._current = initial
        self._root = root
        self._list_fn = list_fn
        self._parent_fn = parent_fn
        self._pick_current = pick_current
        self._preview_fn = preview_fn
        self._selected: list[str] = []
        self._selected_set: set[str] = set()
        self._cache: dict[str, tuple[list[DirEntry], int]] = {}
        self._preview_cache: dict[str, PreviewInfo] = {}
        self._preview_target: str | None = None
        self._base_entries: list[DirEntry] = []
        self._entries: list[DirEntry] = []
        self._match_count = 0  # filtered matches before the render cap
        self._file_count = 0
        self._sort_mode = SORT_MODES[0]
        self._filter = ""
        self._filter_editing = False
        self._filter_draft = ""
        self._filter_timer = None

    @property
    def _show_preview(self) -> bool:
        return self._preview_fn is not None and not self._pick_current

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="path-bar", classes="tui-bar")
        with Horizontal(id="main"):
            with Vertical(id="browser-panel"):
                yield Static("", id="filter-bar")
                yield ListView(id="nav")
            with Vertical(id="selected-panel"):
                if self._show_preview:
                    yield Static("Preview", id="preview-title", classes="tui-title")
                    yield Rule(id="preview-divider")
                    yield Static("[dim]…[/dim]", id="preview-body")
                yield Static("Selected", id="selected-title", classes="tui-title")
                yield Rule(id="selected-divider")
                yield Static("[dim]nothing selected[/dim]", id="selected-items")
        yield Footer()

    def on_mount(self) -> None:
        self._navigate(self._current)

    # ── navigation ────────────────────────────────────────────────────────────

    def _navigate(self, path: str) -> None:
        self._current = path
        if path not in self._cache:
            self._cache[path] = self._list_fn(path)
        self._base_entries, self._file_count = self._cache[path]
        # Filter is per-directory; reset it when moving to a new listing.
        self._filter = ""
        self._filter_editing = False
        self._update_filter_bar()
        self._recompute_display()
        self.title = self._title_prefix
        self._update_path_bar()
        self._sync_selection()

    def _update_path_bar(self) -> None:
        count_tag = (
            f"  [dim]\\[{self._file_count} file"
            f"{'' if self._file_count == 1 else 's'}][/dim]"
            if self._file_count
            else ""
        )
        sort_tag = f"  [dim]sort:{SORT_LABELS[self._sort_mode]}[/dim]"
        flt_tag = (
            f"  [accent]filter:{markup_escape(self._filter)}[/accent]"
            if self._filter
            else ""
        )
        self.query_one("#path-bar", Static).update(
            f"[dim]▸[/dim] {markup_escape(self._current)}{count_tag}"
            f"{sort_tag}{flt_tag}{self._overflow_note()}"
        )

    def _set_entries(self, flt: str) -> None:
        """Sort + filter the base entries, then cap the rendered window."""
        ordered = _apply_sort(self._base_entries, self._sort_mode)
        matches = _apply_filter(ordered, flt)
        self._match_count = len(matches)
        self._entries = matches[:RENDER_CAP]

    def _recompute_display(self) -> None:
        """Apply the active sort and filter to the base entries, then rebuild."""
        self._set_entries(self._filter)
        self._rebuild_list()

    def _row_glyph(self, value: str) -> str:
        if value in self._selected_set:
            return "[green bold]●[/green bold] "
        return "[dim]○[/dim] "

    def _overflow_note(self) -> str:
        hidden = self._match_count - len(self._entries)
        if hidden <= 0:
            return ""
        return (
            f"  [yellow]showing {len(self._entries)}/{self._match_count}"
            f" — narrow the filter to see the rest[/yellow]"
        )

    def _rebuild_list(self) -> None:
        lv = self.query_one("#nav", ListView)
        lv.clear()
        lv.extend(
            ListItem(Label(f"{self._row_glyph(e.value)}{e.label}", markup=True))
            for e in self._entries
        )
        if self._entries:
            lv.index = 0

    def _update_item_label(self, idx: int) -> None:
        e = self._entries[idx]
        mark = self._row_glyph(e.value)
        try:
            items = list(self.query_one("#nav", ListView).query(ListItem))
            items[idx].query_one(Label).update(f"{mark}{e.label}")
        except Exception:
            pass

    def _sync_selection(self) -> None:
        if self._pick_current:
            self.sub_title = ""
            self.query_one("#selected-title", Static).update("Destination")
            panel: list[str] = []
            panel.append("[dim]Output root:[/dim]")
            panel.append(f"[bold]{markup_escape(self._current)}[/bold]")
            existing = count_existing(self._current)
            if existing:
                panel.append(
                    f"[yellow]⚠ exists — {existing} item"
                    f"{'' if existing == 1 else 's'} inside[/yellow]"
                )
            self.query_one("#selected-items", Static).update("\n".join(panel))
            return
        n = len(self._selected)
        self.sub_title = f"{n} selected" if n else ""
        self.query_one("#selected-title", Static).update(
            f"Selected ({n})" if n else "Selected"
        )
        items_widget = self.query_one("#selected-items", Static)
        if self._selected:
            lines = [
                f"[green]●[/green] [bold]{markup_escape(Path(s).name)}[/bold]"
                for s in self._selected
            ]
        else:
            lines = ["[dim]nothing selected[/dim]"]
        items_widget.update("\n".join(lines + self._selection_extra_lines()))

    def _selection_extra_lines(self) -> list[str]:
        """Extra panel lines below the selection (start mode shows the output
        root here)."""
        return []

    # ── key handling ──────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Swallow ListView's own select event (fired by a mouse click) so a
        click only highlights the row + refreshes the preview. Confirming is
        enter / q; selecting a dir is space; entering one is → / l."""
        event.stop()

    # ── preview ────────────────────────────────────────────────────────────────

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if not self._show_preview:
            return
        idx = self.query_one("#nav", ListView).index
        if idx is None or idx >= len(self._entries):
            return
        entry = self._entries[idx]
        if not entry.is_dir:
            # Leaf (file) entry has no directory listing to preview.
            self._preview_target = entry.value
            self.query_one("#preview-body", Static).update(
                f"[dim]{markup_escape(Path(entry.value).name)}[/dim]"
            )
            return
        self._request_preview(entry.value)

    def _request_preview(self, path: str) -> None:
        self._preview_target = path
        cached = self._preview_cache.get(path)
        if cached is not None:
            self._render_preview(cached, path)
        else:
            self.query_one("#preview-body", Static).update("[dim]Loading…[/dim]")
            self._load_preview(path)

    @work(exclusive=True, group="preview")
    async def _load_preview(self, path: str) -> None:
        await asyncio.sleep(0.2)  # debounce rapid cursor movement
        if self._preview_target != path or self._preview_fn is None:
            return
        info = await asyncio.to_thread(self._preview_fn, path)
        self._preview_cache[path] = info
        if self._preview_target == path:
            self._render_preview(info, path)

    def _render_preview(self, info: PreviewInfo, path: str) -> None:
        name = Path(path).name or path
        plural = "" if info.count == 1 else "s"
        partial = " [yellow](partial)[/yellow]" if info.partial else ""
        lines = [
            f"[bold]{markup_escape(name)}/[/bold]  —  {info.count} file{plural},"
            f" {human_size(info.total_bytes)}{partial}",
            "",
        ]
        if not info.sample:
            lines.append("[dim]no files[/dim]")
        for rel, size in info.sample:
            lines.append(
                f"  {markup_escape(rel)}  [dim]{human_size(size)}[/dim]"
            )
        more = info.count - len(info.sample)
        if more > 0:
            lines.append(f"  [dim]+{more} more[/dim]")
        try:
            self.query_one("#preview-body", Static).update("\n".join(lines))
        except Exception:
            pass

    def on_key(self, event: events.Key) -> None:
        # A modal (New dir / Go to) owns the keys — same guard check_action
        # applies to the bindings, else typed names (backspace/l/j/k) navigate
        # the browser underneath (B16).
        if len(self.screen_stack) > 1:
            return
        if self._filter_editing:
            self._handle_filter_key(event)
            return
        # Vim motions only; the arrow keys are handled by the priority bindings
        # above (l/h/j/k aren't bound there, so they route through here).
        lv = self.query_one("#nav", ListView)
        if event.key == "l":
            self.action_enter_dir()
            event.stop()
        elif event.key in ("h", "backspace"):
            self.action_go_up()
            event.stop()
        elif event.key == "j":
            lv.action_cursor_down()
            event.stop()
        elif event.key == "k":
            lv.action_cursor_up()
            event.stop()

    # ── filter ─────────────────────────────────────────────────────────────────

    def _handle_filter_key(self, event: events.Key) -> None:
        key = event.key
        event.stop()  # swallow everything while editing the filter draft
        if key == "enter":
            self._filter = self._filter_draft.strip()
            self._filter_editing = False
            self._update_filter_bar()
            self._recompute_display()
            self._update_path_bar()
        elif key == "escape":
            self._filter_editing = False
            self._update_filter_bar()
            self._recompute_display()
            self._update_path_bar()
        elif key == "backspace":
            self._filter_draft = self._filter_draft[:-1]
            self._update_filter_bar()
            self._schedule_live_filter()
        elif event.character and event.character.isprintable():
            self._filter_draft += event.character
            self._update_filter_bar()
            self._schedule_live_filter()

    def _schedule_live_filter(self) -> None:
        """Coalesce rapid keystrokes into a single rebuild."""
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(FILTER_DEBOUNCE, self._live_filter)

    def _live_filter(self) -> None:
        self._filter_timer = None
        self._set_entries(self._filter_draft.strip())
        self._rebuild_list()
        self._update_filter_bar()

    def _update_filter_bar(self) -> None:
        bar = self.query_one("#filter-bar", Static)
        if self._filter_editing:
            bar.add_class("editing")
            body = (
                markup_escape(self._filter_draft)
                if self._filter_draft
                else "[dim]type to filter · enter accept · esc cancel[/dim]"
            )
            bar.update(f"[accent]/[/accent] {body}{self._overflow_note()}")
        else:
            bar.remove_class("editing")

    # ── actions ───────────────────────────────────────────────────────────────

    def action_go_up(self) -> None:
        if self._current != self._root:
            self._navigate(self._parent_fn(self._current))

    def action_toggle_select(self) -> None:
        lv = self.query_one("#nav", ListView)
        idx = lv.index
        if idx is not None and idx < len(self._entries):
            entry = self._entries[idx]
            if entry.value in self._selected_set:
                self._selected_set.discard(entry.value)
                self._selected.remove(entry.value)
            else:
                self._selected_set.add(entry.value)
                self._selected.append(entry.value)
            self._update_item_label(idx)
            self._sync_selection()

    def action_enter_dir(self) -> None:
        lv = self.query_one("#nav", ListView)
        idx = lv.index
        if idx is not None and idx < len(self._entries):
            entry = self._entries[idx]
            if not entry.is_dir:
                # Leaf (file) entry — not navigable. Select it with Space.
                return
            self._navigate(entry.value)

    def action_add_current(self) -> None:
        if self._current not in self._selected_set:
            self._selected_set.add(self._current)
            self._selected.append(self._current)
            self.notify(f"Added: {Path(self._current).name}")
        else:
            self._selected_set.discard(self._current)
            self._selected.remove(self._current)
            self.notify(f"Removed: {Path(self._current).name}")
        self._sync_selection()

    def action_new_dir(self) -> None:
        def _on_name(name: str | None) -> None:
            if not name:
                return
            new_path = Path(self._current) / name
            try:
                new_path.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                self.notify(f"Already exists: {name}", severity="warning")
                # Navigate into it anyway so the user can confirm it
            except OSError as e:
                self.notify(str(e), severity="error")
                return
            self._cache.pop(self._current, None)
            self._navigate(str(new_path))

        self.push_screen(_NewDirModal(), _on_name)

    def action_cycle_sort(self) -> None:
        idx = SORT_MODES.index(self._sort_mode)
        self._sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
        self._recompute_display()
        self._update_path_bar()

    def action_filter(self) -> None:
        self._filter_editing = True
        self._filter_draft = self._filter
        self._update_filter_bar()
        self._live_filter()

    def action_goto(self) -> None:
        def _on_path(raw: str | None) -> None:
            if not raw:
                return
            path = os.path.expanduser(raw)
            if not path.startswith(("s3://", "/")) and not self._current.startswith(
                "s3://"
            ):
                path = str(Path(self._current) / path)
            self._navigate(path)

        self.push_screen(_GoToModal(self._current), _on_path)

    def action_vim_top(self) -> None:
        if self._entries:
            self.query_one("#nav", ListView).index = 0

    def action_vim_bottom(self) -> None:
        if self._entries:
            self.query_one("#nav", ListView).index = len(self._entries) - 1

    def action_quit_done(self) -> None:
        if self._pick_current:
            self.exit([self._current])
        else:
            self.exit(self._selected)

    def action_abort(self) -> None:
        """Cancel without confirming any selection (returns None)."""
        self.exit(None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """Hide select-oriented bindings in single-dir picker mode, and disable
        all key bindings while the filter draft is being edited or a modal
        (New dir / Go to) is open. The bindings are priority, so without this
        they would fire before the modal's focused Input and steal its keys
        (e.g. space, n, q, Enter, Esc) instead of typing into it."""
        if self._filter_editing:
            return False
        if len(self.screen_stack) > 1:
            return False
        if self._pick_current and action in ("toggle_select", "add_current"):
            return False
        return True


class StartBrowserApp(BrowserApp):
    """Source browser whose confirm starts the command's work directly (V103).

    There is no follow-up destination picker app: `s` and Enter both return
    the selection (footer says "Start") and `q` joins Esc as cancel. With
    nothing toggled, the highlighted dir becomes the single source (V104).
    `S` flips this SAME app into destination mode (V106): the listing swaps to
    the local filesystem at the default destination, `s`/Enter then mean
    "start HERE" (the current dir becomes the output root), Esc backs out to
    the source view. Exit value is ``(sources, dest | None, mark | None)`` —
    dest None means the caller's default destination; mark is only ever set
    without dest fns.

    Without dest fns (video) there is no destination mode: `S` is hidden and
    starting returns the CURRENT dir as dest — start-in-place, the directory
    being browsed is the destination. `d` instead marks the highlighted dir
    as the output root (the output mark): at most one dir carries it, `d` on
    the marked dir removes it, `d` elsewhere moves it, and a dir cannot be
    both a selected source and the mark — the newer act displaces the older.
    """

    BINDINGS = [
        Binding("enter", "quit_done", "Start", priority=True),
        Binding("s", "quit_done", "Start", priority=True),
        Binding("S", "choose_dest", "Dest", priority=True),
        Binding("d", "mark_output", "Output", priority=True),
        Binding("q", "cancel", "Quit", priority=True),
    ]

    def __init__(
        self,
        title_prefix: str,
        initial: str,
        root: str,
        list_fn: Callable[[str], tuple[list[DirEntry], int]],
        parent_fn: Callable[[str], str],
        dest_list_fn: Callable[[str], tuple[list[DirEntry], int]] | None = None,
        dest_parent_fn: Callable[[str], str] | None = None,
        dest_initial: str | None = None,
        preview_fn: Callable[[str], PreviewInfo] | None = None,
    ) -> None:
        super().__init__(
            title_prefix, initial, root, list_fn, parent_fn,
            preview_fn=preview_fn,
        )
        self._dest_list_fn = dest_list_fn
        self._dest_parent_fn = dest_parent_fn
        self._dest_initial = dest_initial
        self._source_fns = (list_fn, parent_fn)
        self._dest_mode = False
        self._pending_sources: list[str] = []
        self._source_state: tuple[str, str] | None = None
        self._output_mark: str | None = None

    def _sources_to_start(self) -> list[str] | None:
        if self._selected:
            return list(self._selected)
        idx = self.query_one("#nav", ListView).index
        if idx is None or idx >= len(self._entries):
            return None  # empty listing and nothing selected
        return [self._entries[idx].value]

    @property
    def _has_dest_mode(self) -> bool:
        return self._dest_list_fn is not None

    def action_quit_done(self) -> None:
        if self._dest_mode:
            self.exit((self._pending_sources, self._current, None))
            return
        sources = self._sources_to_start()
        if sources is None:
            return
        if self._has_dest_mode:
            # None = caller's default destination.
            self.exit((sources, None, None))
            return
        # No destination mode (video) → the mark is the output root, else
        # start-in-place: the dir being browsed is the destination. The
        # implicit highlighted-dir source (empty selection) must not be the
        # mark — outputs share the input filenames and would clobber them.
        if not self._selected and sources == [self._output_mark]:
            self.notify(
                "Marked output dir cannot be a source", severity="warning"
            )
            return
        self.exit((sources, self._current, self._output_mark))

    def action_mark_output(self) -> None:
        lv = self.query_one("#nav", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._entries):
            return
        entry = self._entries[idx]
        if not entry.is_dir:
            # Output root must be a directory; a leaf (file) can't be marked.
            self.notify("Can't mark a file as the output root.", severity="warning")
            return
        value = entry.value
        old = self._output_mark
        if old == value:
            self._output_mark = None
        else:
            self._output_mark = value
            # A dir cannot be both a source and the mark — displace selection.
            if value in self._selected_set:
                self._selected_set.discard(value)
                self._selected.remove(value)
            if old is not None:
                self._refresh_row(old)
        self._update_item_label(idx)
        self._sync_selection()

    def _refresh_row(self, value: str) -> None:
        for i, e in enumerate(self._entries):
            if e.value == value:
                self._update_item_label(i)
                return

    def action_toggle_select(self) -> None:
        lv = self.query_one("#nav", ListView)
        idx = lv.index
        # Selecting the marked dir displaces the mark (the newer act wins).
        if (
            idx is not None
            and idx < len(self._entries)
            and self._entries[idx].value == self._output_mark
        ):
            self._output_mark = None
        super().action_toggle_select()

    def action_add_current(self) -> None:
        if self._current == self._output_mark:
            self._output_mark = None
        super().action_add_current()

    def _row_glyph(self, value: str) -> str:
        if value == self._output_mark:
            return "[magenta bold]◆[/magenta bold] "
        return super()._row_glyph(value)

    def _selection_extra_lines(self) -> list[str]:
        if self._has_dest_mode:
            return []
        lines = ["", "[dim]Output root:[/dim]"]
        if self._output_mark is not None:
            lines.append(
                f"[magenta bold]◆[/magenta bold] "
                f"[bold]{markup_escape(self._output_mark)}[/bold]"
            )
        else:
            lines.append(
                f"[dim]start-in-place: {markup_escape(self._current)}[/dim]"
            )
        return lines

    def action_choose_dest(self) -> None:
        if not self._has_dest_mode:
            return
        assert self._dest_initial is not None
        sources = self._sources_to_start()
        if sources is None:
            return
        self._pending_sources = sources
        self._source_state = (self._current, self._root)
        self._dest_mode = True
        self._pick_current = True  # single-dir semantics + Destination panel
        self._root = "/"  # the destination is always on the local filesystem
        self._list_fn, self._parent_fn = self._dest_list_fn, self._dest_parent_fn
        # Cached listings belong to the source fs (pod/S3), not the local one.
        self._cache.clear()
        self._set_preview_visible(False)
        # Created only now, so cancelling from the source view never leaves an
        # empty default-destination dir behind.
        Path(self._dest_initial).mkdir(parents=True, exist_ok=True)
        self._navigate(self._dest_initial)

    def action_abort(self) -> None:
        if not self._dest_mode:
            self.exit(None)
            return
        assert self._source_state is not None
        current, root = self._source_state
        self._dest_mode = False
        self._pick_current = False
        self._root = root
        self._list_fn, self._parent_fn = self._source_fns
        self._cache.clear()
        self._set_preview_visible(True)
        self._navigate(current)

    def action_cancel(self) -> None:
        self.exit(None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "choose_dest" and (self._dest_mode or not self._has_dest_mode):
            return False
        # The output mark exists only in browsers without destination mode
        # (video); fetch keeps `S` destination mode instead.
        if action == "mark_output" and self._has_dest_mode:
            return False
        return super().check_action(action, parameters)

    def _set_preview_visible(self, visible: bool) -> None:
        if self._preview_fn is None:
            return  # preview widgets were never composed
        for wid in ("#preview-title", "#preview-divider", "#preview-body"):
            self.query_one(wid).display = visible

    def _sync_selection(self) -> None:
        if not self._dest_mode:
            super()._sync_selection()
            return
        self.sub_title = ""
        self.query_one("#selected-title", Static).update("Destination")
        panel = [f"[dim]Routing {len(self._pending_sources)} dir(s) here:[/dim]"]
        panel += [
            f"  [cyan]{markup_escape(Path(s).name)}[/cyan]"
            for s in self._pending_sources
        ]
        panel.append("")
        panel.append("[dim]Output root:[/dim]")
        panel.append(f"[bold]{markup_escape(self._current)}[/bold]")
        existing = count_existing(self._current)
        if existing:
            panel.append(
                f"[yellow]⚠ exists — {existing} item"
                f"{'' if existing == 1 else 's'} inside[/yellow]"
            )
        self.query_one("#selected-items", Static).update("\n".join(panel))


def run_browser(
    title_prefix: str,
    initial: str,
    root: str,
    list_fn: Callable[[str], tuple[list[DirEntry], int]],
    parent_fn: Callable[[str], str],
    pick_current: bool = False,
    preview_fn: Callable[[str], PreviewInfo] | None = None,
) -> list[str] | None:
    """Run the interactive directory browser and return the selected paths.

    When pick_current=True, q confirms the current navigated path (single-dir
    picker mode — right panel shows the path live, no toggle selection needed).

    When preview_fn is given (selection mode only), the right panel shows a
    recursive file/size preview of the highlighted directory, loaded lazily in
    a background worker.

    Returns the selected paths, an empty list if the user confirmed nothing,
    or None if the user aborted (Esc or q).
    """
    app = BrowserApp(
        title_prefix, initial, root, list_fn, parent_fn, pick_current,
        preview_fn,
    )
    return app.run()


def run_start_browser(
    title_prefix: str,
    initial: str,
    root: str,
    list_fn: Callable[[str], tuple[list[DirEntry], int]],
    parent_fn: Callable[[str], str],
    dest_list_fn: Callable[[str], tuple[list[DirEntry], int]] | None = None,
    dest_parent_fn: Callable[[str], str] | None = None,
    dest_initial: str | None = None,
    preview_fn: Callable[[str], PreviewInfo] | None = None,
) -> tuple[list[str], str | None, str | None] | None:
    """Run the start-mode source browser (V103/V106).

    Returns ``(sources, dest, mark)``: dest is the output root picked in
    destination mode (`S`), or None when the user started straight away
    (caller's default destination applies). Without dest fns there is no
    destination mode and dest is always the directory being browsed at start
    (start-in-place); mark is the output mark set with `d` (None when unset,
    and always None with dest fns). Returns None when the user cancelled.
    """
    app = StartBrowserApp(
        title_prefix, initial, root, list_fn, parent_fn,
        dest_list_fn, dest_parent_fn, dest_initial, preview_fn,
    )
    return app.run()


class MultiSelectApp(App[list[str]]):
    """fzf-style flat multi-select over a fixed list.

    Unlike the directory browser, the query box is always visible and live: you
    type to narrow the list and toggle selections *at the same time* (no modal
    "accept the filter first" step). The selection survives query changes, so
    you can filter to one batch, pick it, retype, and pick another.

    Keys: type to filter · ``space``/``tab`` toggle the highlighted row ·
    ``ctrl+a`` toggle every match · ``↑``/``↓`` (or ``ctrl+p``/``ctrl+n``) move ·
    ``enter`` confirm · ``esc`` clear the query, or cancel when it is empty.
    """

    DEFAULT_CSS = TUI_CSS + """
    #query-bar {
        height: auto;
        background: $panel;
        color: $accent;
        padding: 0 1;
        border-bottom: solid $accent;
    }

    #main {
        height: 1fr;
        margin: 0 1 1 1;
        border: round $primary;
    }

    #list-panel {
        width: 2fr;
        border-right: solid $primary;
    }

    #nav {
        height: 1fr;
    }

    #selected-panel {
        width: 1fr;
        padding: 0 2;
        background: $panel;
    }

    #selected-title {
        height: 1;
        padding: 1 0 0 0;
        text-style: bold;
        color: $accent;
    }

    #selected-divider {
        color: $accent;
        margin: 0;
        height: 1;
    }

    #selected-items {
        padding: 1 0;
        color: $foreground;
    }
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("space", "toggle", "Select", priority=True),
        Binding("tab", "toggle", "Select", priority=True),
        Binding("ctrl+a", "select_all", "All/None", priority=True),
        Binding("escape", "clear_or_abort", "Clear/Cancel", priority=True),
        # SIGINT never reaches a raw-mode TUI (textual traps ctrl+c into a
        # help_quit hint) — so ctrl+c as abort is strictly better UX (B19).
        Binding("ctrl+c", "abort", "Quit", priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("ctrl+n", "cursor_down", "Down", show=False, priority=True),
        Binding("ctrl+p", "cursor_up", "Up", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        choices: list[tuple[str, str]],
        *,
        auto_advance: bool = True,
        live_filter: bool = True,
    ) -> None:
        super().__init__()
        self._title = title
        # entries preserve the caller's order; label is the raw display text
        # (matched against, escaped only at render time).
        self._all: list[DirEntry] = [
            DirEntry(label=label, value=value) for label, value in choices
        ]
        self._matches: list[DirEntry] = list(self._all)  # full filtered set
        self._entries: list[DirEntry] = []               # rendered window
        self._selected_set: set[str] = set()
        self._selected_order: list[str] = []
        self._query = ""
        self._filter_timer = None
        # auto_advance: space/tab moves the cursor to the next row after a
        # toggle. live_filter: every printable key feeds the query (fzf-style);
        # when off, `/` opens query editing, enter accepts, esc clears — and
        # j/k navigate while the query is closed.
        self._auto_advance = auto_advance
        self._live_filter = live_filter
        self._query_editing = live_filter

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="query-bar")
        with Horizontal(id="main"):
            with Vertical(id="list-panel"):
                yield ListView(id="nav")
            with Vertical(id="selected-panel"):
                yield Static("Selected", id="selected-title")
                yield Rule(id="selected-divider")
                yield Static("[dim]nothing selected[/dim]", id="selected-items")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._title
        self._recompute()
        self._update_query_bar()
        self._sync_selection()
        self.query_one("#nav", ListView).focus()

    # ── filtering ───────────────────────────────────────────────────────────

    def _recompute(self) -> None:
        flt = self._query.strip()
        self._matches = (
            list(self._all)
            if not flt
            else [e for e in self._all if _text_matches(e.label, flt)]
        )
        self._entries = self._matches[:RENDER_CAP]
        self._rebuild_list()

    def _schedule_filter(self) -> None:
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(FILTER_DEBOUNCE, self._apply_query)

    def _apply_query(self) -> None:
        self._filter_timer = None
        self._recompute()
        self._update_query_bar()

    def _mark(self, value: str) -> str:
        return "[green bold]●[/green bold] " if value in self._selected_set else "[dim]○[/dim] "

    def _rebuild_list(self) -> None:
        lv = self.query_one("#nav", ListView)
        lv.clear()
        lv.extend(
            ListItem(Label(f"{self._mark(e.value)}{markup_escape(e.label)}", markup=True))
            for e in self._entries
        )
        if self._entries:
            lv.index = 0

    def _update_item_label(self, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        e = self._entries[idx]
        try:
            items = list(self.query_one("#nav", ListView).query(ListItem))
            items[idx].query_one(Label).update(
                f"{self._mark(e.value)}{markup_escape(e.label)}"
            )
        except Exception:
            pass

    def _update_query_bar(self) -> None:
        hidden = len(self._matches) - len(self._entries)
        overflow = (
            f"  [yellow]showing {len(self._entries)}/{len(self._matches)}"
            f" — keep typing to narrow[/yellow]"
            if hidden > 0
            else ""
        )
        if self._query_editing:
            cursor = "[accent]▏[/accent]"
            body = markup_escape(self._query) or "[dim]type to filter[/dim]"
        else:
            cursor = ""
            body = markup_escape(self._query) or "[dim]press / to filter[/dim]"
        counts = (
            f"  [dim]{len(self._matches)}/{len(self._all)}"
            f" · {len(self._selected_order)} selected[/dim]"
        )
        self.query_one("#query-bar", Static).update(
            f"[accent]🔎[/accent] {body}{cursor}{counts}{overflow}"
        )

    def _sync_selection(self) -> None:
        n = len(self._selected_order)
        self.sub_title = f"{n} selected" if n else ""
        self.query_one("#selected-title", Static).update(
            f"Selected ({n})" if n else "Selected"
        )
        widget = self.query_one("#selected-items", Static)
        if self._selected_order:
            label_by_value = {e.value: e.label for e in self._all}
            widget.update(
                "\n".join(
                    f"[green]●[/green] {markup_escape(label_by_value.get(v, v))}"
                    for v in self._selected_order
                )
            )
        else:
            widget.update("[dim]nothing selected[/dim]")

    # ── key handling ────────────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:
        # space/tab/enter/esc/arrows are handled by (priority) bindings; here we
        # only grow/shrink the live query from printable input.
        if not self._query_editing:
            # query closed (live_filter off): `/` opens it, j/k navigate.
            if event.key == "slash":
                self._query_editing = True
                self._update_query_bar()
                event.stop()
            elif event.key == "j":
                self.action_cursor_down()
                event.stop()
            elif event.key == "k":
                self.action_cursor_up()
                event.stop()
            return
        if event.key == "backspace":
            if self._query:
                self._query = self._query[:-1]
                self._update_query_bar()
                self._schedule_filter()
            event.stop()
        elif event.character and event.character.isprintable() and event.key != "space":
            self._query += event.character
            self._update_query_bar()
            self._schedule_filter()
            event.stop()

    # ── actions ─────────────────────────────────────────────────────────────

    def action_cursor_down(self) -> None:
        self.query_one("#nav", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#nav", ListView).action_cursor_up()

    def _toggle_value(self, value: str) -> None:
        if value in self._selected_set:
            self._selected_set.discard(value)
            self._selected_order.remove(value)
        else:
            self._selected_set.add(value)
            self._selected_order.append(value)

    def action_toggle(self) -> None:
        lv = self.query_one("#nav", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._entries)):
            return
        self._toggle_value(self._entries[idx].value)
        self._update_item_label(idx)
        self._update_query_bar()
        self._sync_selection()
        # auto-advance so repeated space/tab picks a run of rows quickly
        if self._auto_advance and idx < len(self._entries) - 1:
            lv.action_cursor_down()

    def action_select_all(self) -> None:
        """Toggle every current match: select all, or clear them if all are
        already selected. Operates on the full filtered set, not just the
        rendered window."""
        values = [e.value for e in self._matches]
        if values and all(v in self._selected_set for v in values):
            for v in values:
                self._selected_set.discard(v)
                if v in self._selected_order:
                    self._selected_order.remove(v)
        else:
            for v in values:
                if v not in self._selected_set:
                    self._selected_set.add(v)
                    self._selected_order.append(v)
        for idx in range(len(self._entries)):
            self._update_item_label(idx)
        self._update_query_bar()
        self._sync_selection()

    def action_confirm(self) -> None:
        # live_filter off: enter while the query is open accepts it (stays
        # filtered) instead of confirming the selection.
        if not self._live_filter and self._query_editing:
            self._query_editing = False
            if self._filter_timer is not None:
                self._filter_timer.stop()
                self._filter_timer = None
            self._recompute()
            self._update_query_bar()
            return
        if self._selected_order:
            self.exit(list(self._selected_order))
            return
        # nothing explicitly toggled → confirm the highlighted row (fzf-style)
        lv = self.query_one("#nav", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self._entries):
            self.exit([self._entries[idx].value])
        else:
            self.exit([])

    def action_clear_or_abort(self) -> None:
        # live_filter off: esc while the query is open closes it and clears.
        if not self._live_filter and self._query_editing:
            self._query_editing = False
            self._query = ""
            self._recompute()
            self._update_query_bar()
            return
        if self._query:
            self._query = ""
            self._recompute()
            self._update_query_bar()
        else:
            self.exit(None)

    def action_abort(self) -> None:
        self.exit(None)


def run_multi_select(
    title: str,
    choices: list[tuple[str, str]],
    *,
    auto_advance: bool = True,
    live_filter: bool = True,
) -> list[str] | None:
    """fzf-style flat multi-select over a fixed list.

    choices = [(label, value), ...]. Type to filter live; space/tab toggle the
    highlighted row (you can select while filtering); ctrl+a toggles every
    match; enter confirms (the highlighted row if nothing is toggled); esc
    clears the query or cancels when it is empty. Returns selected values in
    selection order, an empty list if nothing was confirmed, or None if the
    user aborted.

    auto_advance=False keeps the cursor on the toggled row instead of moving to
    the next one. live_filter=False starts with the query closed: printable
    keys no longer filter (j/k navigate instead) until ``/`` opens the query —
    enter accepts it, esc closes and clears it.
    """
    return MultiSelectApp(
        title, choices, auto_advance=auto_advance, live_filter=live_filter
    ).run()

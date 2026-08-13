# textual-dirbrowser

A [Textual](https://textual.textualize.io/) TUI directory browser with **injectable listing backends**.
The browser knows nothing about storage: local filesystem, S3, a remote pod —
any of them is just a pair of callbacks you supply.

## Install

```sh
uv add "textual-dirbrowser @ git+ssh://git@github.com/kacperkan/textual-dirbrowser@v0.1.0"
```

## The backend contract

Every entry point takes:

- `list_fn(path) -> (entries, file_count)` — directory listing as
  `list[DirEntry]` (newest-first; the mtime sort modes rely on that order)
  plus the number of plain files in the directory.
- `parent_fn(path) -> str` — the parent of `path` in your namespace.

`DirEntry(label, value, is_dir=True)`: `label` is the display text (Rich markup
allowed), `value` the opaque path handle passed back to your callbacks and
returned from the browse. `is_dir` defaults to `True`; set it `False` to emit a
**leaf** entry (e.g. a file) — a leaf can be Space-selected but is never
navigated into (`→`/`l` is a no-op) and can't be a `d` output mark. This lets a
`list_fn` return files alongside directories in the same listing.

## Entry points

### `run_browser(...)` — multi-select directory browser

```python
from pathlib import Path
from textual_dirbrowser import DirEntry, run_browser

def list_fn(path: str) -> tuple[list[DirEntry], int]:
    p = Path(path)
    dirs = sorted((d for d in p.iterdir() if d.is_dir()), key=lambda d: -d.stat().st_mtime)
    files = sum(1 for f in p.iterdir() if f.is_file())
    return [DirEntry(label=d.name, value=str(d)) for d in dirs], files

selected = run_browser(
    "pick datasets", initial=str(Path.home()), root="/",
    list_fn=list_fn, parent_fn=lambda p: str(Path(p).parent),
)
# list of selected paths, [] if confirmed empty, None if cancelled
```

Keys: arrows/`hjkl` navigate · `space` select · `a` add current dir ·
`/` filter (substring or glob) · `o` cycle sort · `:` go to path ·
`n` new dir · `enter` confirm · `q`/`esc` cancel.

Optional: `pick_current=True` turns it into a single-dir picker;
`preview_fn(path) -> PreviewInfo` adds a recursive size/sample preview pane.

### `run_start_browser(...)` — source picker that starts work directly

Same browsing keys; `s`/`enter` "start" and return
`(sources, dest, mark)`. With `dest_list_fn`/`dest_parent_fn`/`dest_initial`
given, `S` flips the same window into **destination mode** to choose a local
output root. Without them, `d` sets the **output mark** and starting in place
uses the browsed directory as destination. Returns `None` on cancel.

Destination mode is local-filesystem by design (it creates directories and
counts existing entries with `pathlib`).

### `run_multi_select(title, choices)` — fzf-style flat picker

```python
from textual_dirbrowser import run_multi_select

picked = run_multi_select("choose jobs", [("job-a (running)", "job-a"), ("job-b", "job-b")])
```

Type to filter live · `space`/`tab` toggle · `ctrl+a` toggle all matches ·
`enter` confirm (highlighted row if nothing toggled) · `esc` clear query /
cancel · `ctrl+c` abort.

Two keyword tweaks (0.3.0): `auto_advance=False` keeps the cursor on the
toggled row instead of jumping to the next one; `live_filter=False` starts
with the query closed — printable keys stop filtering (`j`/`k` navigate
instead) until `/` opens the query, `enter` accepts it, `esc` closes and
clears it.

## Restyling

The App classes (`BrowserApp`, `StartBrowserApp`, `MultiSelectApp`) are
public; subclass and override `DEFAULT_CSS` to restyle. The default theme
lives in `textual_dirbrowser.css` as `TUI_CSS` / `MODAL_CSS`.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

## How this repo was made

Built in a single [Claude Code](https://claude.com/claude-code) session,
using two skills:

- **grill-with-docs** — a relentless design interview that walked every branch
  of the design (scope, naming, API surface, CSS ownership, hosting) one
  question at a time. Its outputs live here as `CONTEXT.md` (glossary) and
  `docs/adr/0001-ui-only-boundary.md` (decisions + rejected alternatives).
- **karpathy-guidelines** — behavioral rules for the implementation itself:
  surgical changes only, no speculative features, every step verified by the
  test suite before moving on.

### House rule

Do not work on this repo without those two skills. Design changes go through a
grill session first (and update `CONTEXT.md` / add an ADR); code changes follow
the karpathy guidelines — minimal diff, tests green before and after. PRs that
skip the process get grilled retroactively, which is worse.

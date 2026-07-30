# 2. Leaf (non-navigable) entries via `DirEntry.is_dir`

Date: 2026-07-30

## Status

Accepted

## Context

The browser was directory-only: every `DirEntry` was assumed navigable, so
`→`/`l` called `list_fn(entry.value)` on it and `d` could mark it as the output
root. A consumer that wants to list **files** alongside directories (to pick
individual files, not just dirs) had no way to say "this entry is a leaf" — a
`list_fn` that returned a file entry would crash the browser as soon as the
cursor entered it (`list_fn` runs `iterdir()` on a file → `NotADirectoryError`),
and nothing stopped `d` from marking a file as an output directory.

## Decision

`DirEntry` gains `is_dir: bool = True`.

- **Default `True`** keeps every existing (all-directory) caller unchanged —
  the field is optional and the old two-arg construction still works.
- **`is_dir=False` marks a leaf.** A leaf can be Space-selected (selection is
  value-based, unchanged) but:
  - `→`/`l` (`action_enter_dir`) is a no-op — never navigates into it.
  - `d` (`action_mark_output`) refuses it with a notification — the output
    root must be a directory.
  - the preview pane shows the leaf's name instead of trying to list it.

Homogeneity (all-leaf vs all-dir selections, mixing rules) is left to the
**host** — the package only provides the leaf primitive.

## Consequences

- A `list_fn` can now return a mixed listing of directories and files; hosts
  decide what a file selection means.
- Callers relying on "every entry is navigable" are unaffected (default).
- The browser stays UI-only ([ADR 1](0001-ui-only-boundary.md)): it enforces no
  policy on *which* combinations of leaves/dirs a host accepts.

# textual-dirbrowser — Ubiquitous Language

Reusable Textual TUI directory browser. This glossary covers the browser's
own concepts only; host-application concepts stay in the host's own docs.

## Terms

**Backend**:
The pair of caller-supplied callbacks (`list_fn`, `parent_fn`) that resolve
directory listings and parent paths. The browser has no filesystem, S3, or
pod knowledge of its own — every storage flavor is a backend injected by the
host application.
_Avoid_: Filesystem layer, storage driver, mode

**Host application**:
The program embedding the browser. It supplies backends, titles, optional
preview and CSS, and consumes the returned selection.
_Avoid_: Client, caller-app

**Highlighted directory**:
The directory the browser cursor currently rests on. Highlighting only moves
focus and refreshes the directory preview; it neither selects nor opens.
_Avoid_: Selected directory, active directory

**Directory selection**:
Marking a highlighted directory to be included in the result batch. Selection
is a toggle and accumulates across navigation, producing the set of
directories returned to the host application. It is distinct from starting
or entering.
_Avoid_: Confirm, open, enter

**Enter directory**:
Navigating into a highlighted subdirectory to browse its contents. Entering
changes the listing only; it does not select the directory or end the browse.
_Avoid_: Select, confirm, open

**Start**:
Deliberately ending the browse from inside the source view (`s`, or Enter as
its alias) and handing the accumulated directory selection back to the host
application. With an empty selection, the highlighted directory becomes the
single result — unless it carries the output mark, in which case starting is
refused with a notice. Quitting (`q`/Esc from the source view) ends the
browse with nothing returned.
_Avoid_: Confirm browse, done-and-select, quit-done

**Destination mode**:
The source browser flipped in place onto a second backend (typically the
local filesystem) to choose an output root before starting. Available only
when the host supplies a destination backend. Backing out returns to the
source view with selections intact. It is the same window — not a second
browser.
_Avoid_: Destination picker, second browser, save dialog

**Output mark**:
Designating the highlighted directory as the output root from inside the
source view. At most one directory carries the mark: marking it again
removes it, marking another moves it. A directory cannot be both a selected
source and the output mark — the newer act displaces the older.
_Avoid_: Destination mode, output selection, save target

**Directory preview**:
A live panel showing the recursive file sample, total file count, and total
size of the highlighted directory, computed by an optional host-supplied
preview callback.
_Avoid_: File list, ls pane

**Flat multi-select**:
The fzf-style picker over a fixed list of labeled choices (`run_multi_select`)
— no navigation, no backends, just filter-and-toggle over the given items.
_Avoid_: Browser, menu

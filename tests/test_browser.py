"""Tests for textual_dirbrowser.browser pure helpers (sort, filter, entry-name, sizes)."""
from textual_dirbrowser.browser import (
    FILTER_DEBOUNCE,
    DirEntry,
    _apply_filter,
    _apply_sort,
    _entry_name,
    human_size,
)


def _entries(*names):
    return [DirEntry(label=n, value=f"/root/{n}") for n in names]


def _names(entries):
    return [_entry_name(e.value) for e in entries]


# ── _entry_name ──────────────────────────────────────────────────────────────

def test_entry_name_plain():
    assert _entry_name("/root/exp1") == "exp1"


def test_entry_name_trailing_slash():
    assert _entry_name("/root/exp1/") == "exp1"


def test_entry_name_s3():
    assert _entry_name("s3://bucket/data/run42/") == "run42"


# ── _apply_sort ──────────────────────────────────────────────────────────────

def test_sort_mtime_desc_is_identity():
    # list_fn already returns newest-first; mtime_desc preserves that order.
    ents = _entries("c", "a", "b")
    assert _names(_apply_sort(ents, "mtime_desc")) == ["c", "a", "b"]


def test_sort_mtime_asc_reverses():
    ents = _entries("c", "a", "b")
    assert _names(_apply_sort(ents, "mtime_asc")) == ["b", "a", "c"]


def test_sort_name_asc():
    ents = _entries("c", "a", "b")
    assert _names(_apply_sort(ents, "name_asc")) == ["a", "b", "c"]


def test_sort_name_desc():
    ents = _entries("c", "a", "b")
    assert _names(_apply_sort(ents, "name_desc")) == ["c", "b", "a"]


def test_sort_does_not_mutate_input():
    ents = _entries("c", "a", "b")
    _apply_sort(ents, "name_asc")
    assert _names(ents) == ["c", "a", "b"]


# ── _apply_filter ────────────────────────────────────────────────────────────

def test_filter_empty_returns_all():
    ents = _entries("alpha", "beta")
    assert _names(_apply_filter(ents, "")) == ["alpha", "beta"]


def test_filter_substring_case_insensitive():
    ents = _entries("Alpha", "beta", "alphabet")
    assert _names(_apply_filter(ents, "AL")) == ["Alpha", "alphabet"]


def test_filter_glob():
    ents = _entries("alpha", "alphabet", "beta")
    assert _names(_apply_filter(ents, "a*a")) == ["alpha"]


def test_filter_glob_question_mark():
    ents = _entries("a1", "a2", "a12")
    assert _names(_apply_filter(ents, "a?")) == ["a1", "a2"]


# ── human_size ───────────────────────────────────────────────────────────────

def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024**2) == "1.0 MB"


# ── render cap ───────────────────────────────────────────────────────────────
#
# ListView is not virtualised, so the browser caps how many rows it mounts.
# Filtering still runs over the full listing; only the rendered window is
# capped (otherwise a listing with thousands of entries freezes the UI for
# seconds on every keystroke).

def _big_app(n):
    from textual_dirbrowser.browser import BrowserApp

    ents = [DirEntry(label=f"job-{i:05d}", value=f"/root/done/job-{i:05d}.sh")
            for i in range(n)]
    return BrowserApp(
        "t", "", "",
        list_fn=lambda _p: (list(ents), 0),
        parent_fn=lambda p: p,
    )


def test_set_entries_caps_rendered_window():
    from textual_dirbrowser.browser import RENDER_CAP

    app = _big_app(RENDER_CAP + 200)
    app._base_entries, _ = app._list_fn("")
    app._set_entries("")
    # full match count is preserved, but only RENDER_CAP rows are rendered
    assert app._match_count == RENDER_CAP + 200
    assert len(app._entries) == RENDER_CAP


def test_set_entries_no_cap_when_under_limit():
    app = _big_app(10)
    app._base_entries, _ = app._list_fn("")
    app._set_entries("")
    assert app._match_count == 10
    assert len(app._entries) == 10
    assert app._overflow_note() == ""


def test_overflow_note_reports_hidden_matches():
    from textual_dirbrowser.browser import RENDER_CAP

    app = _big_app(RENDER_CAP + 49)
    app._base_entries, _ = app._list_fn("")
    app._set_entries("")
    note = app._overflow_note()
    assert f"{RENDER_CAP}/{RENDER_CAP + 49}" in note
    assert "narrow the filter" in note


def test_filtering_narrows_below_cap():
    from textual_dirbrowser.browser import RENDER_CAP

    app = _big_app(RENDER_CAP + 500)
    app._base_entries, _ = app._list_fn("")
    # a specific filter matches a single entry even though the listing is huge
    app._set_entries("job-00042")
    assert app._match_count == 1
    assert len(app._entries) == 1
    assert app._overflow_note() == ""


def test_large_listing_filter_does_not_freeze():
    """Regression: typing in the filter over thousands of entries used to mount
    one widget per entry per keystroke (multi-second freeze). The render cap
    keeps each rebuild bounded."""
    import asyncio
    from textual_dirbrowser.browser import RENDER_CAP

    async def run():
        app = _big_app(4000)
        async with app.run_test() as pilot:
            await pilot.pause()
            # startup renders at most the cap, not all 4000 entries
            assert len(app._entries) == RENDER_CAP
            assert app._match_count == 4000
            await pilot.press("slash")
            for ch in "job-001":
                await pilot.press(ch)
            await pilot.pause()
            await asyncio.sleep(FILTER_DEBOUNCE + 0.05)
            await pilot.pause()
            # "job-001" matches job-00100..00199 -> 100 entries (under the cap,
            # so all are rendered) instead of the full 4000.
            assert app._filter_draft == "job-001"
            assert app._match_count == 100
            assert len(app._entries) == 100

    asyncio.run(run())


# ── directory browser keybindings ─────────────────────────────────────────────
#
# The focused ListView ships its own bindings (enter→select_cursor and, via its
# scroll-container base, left/right→horizontal scroll). Those shadow the app's
# bindings unless the app marks them priority. These tests guard that enter
# confirms, space selects, the arrows navigate (not scroll), and a mouse click
# only highlights — i.e. the app keymap wins over the widget defaults.

def _nav_app(initial="/root"):
    from textual_dirbrowser.browser import BrowserApp

    tree = {
        "/root": [DirEntry(label="exp1", value="/root/exp1"),
                  DirEntry(label="exp2", value="/root/exp2")],
        "/root/exp1": [DirEntry(label="sub", value="/root/exp1/sub")],
        "/root/exp2": [],
    }
    return BrowserApp(
        "t", initial, "/root",
        list_fn=lambda p: (list(tree.get(p, [])), 0),
        parent_fn=lambda _p: "/root",
    )


def test_browser_all_bindings_are_priority():
    """Every browser binding must be a priority Binding; a plain tuple means the
    focused ListView's default for that key wins and the browser action is dead."""
    from textual.binding import Binding

    from textual_dirbrowser.browser import BrowserApp

    bindings = BrowserApp.BINDINGS
    assert all(isinstance(b, Binding) for b in bindings)
    assert all(b.priority for b in bindings)
    keys = {b.key for b in bindings}
    assert {"enter", "left", "right", "space"} <= keys


def test_browser_enter_confirms_selection():
    import asyncio

    async def run():
        app = _nav_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # toggle the highlighted exp1
            await pilot.pause()
            await pilot.press("enter")   # confirm + exit
            await pilot.pause()
        assert app.return_value == ["/root/exp1"]

    asyncio.run(run())


def test_browser_space_toggles_selection():
    import asyncio

    async def run():
        app = _nav_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            assert app._selected == ["/root/exp1"]
            await pilot.press("space")   # toggling again clears it
            await pilot.pause()
            assert app._selected == []

    asyncio.run(run())


def test_browser_right_arrow_enters_directory():
    """Regression: ListView's base binds right→scroll_right, so without a
    priority binding the arrow scrolled instead of entering the directory."""
    import asyncio

    async def run():
        app = _nav_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current == "/root"
            await pilot.press("right")   # enter the highlighted exp1
            await pilot.pause()
        assert app._current == "/root/exp1"

    asyncio.run(run())


def test_browser_left_arrow_goes_up():
    """Regression: left→scroll_left on the ListView base shadowed go-up."""
    import asyncio

    async def run():
        app = _nav_app(initial="/root/exp1")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current == "/root/exp1"
            await pilot.press("left")    # back up to /root
            await pilot.pause()
        assert app._current == "/root"

    asyncio.run(run())


def test_browser_click_highlights_without_confirming():
    """A single mouse click highlights a row but must not confirm-and-exit; only
    enter / q do. The old handler quit on click (returning the empty selection);
    now the click is swallowed so a later escape can abort to None."""
    import asyncio

    from textual.widgets import ListItem

    async def run():
        app = _nav_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click(ListItem)  # click first row
            await pilot.pause()
            # reached only because the click did not exit the app
            await pilot.press("escape")
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_browser_modal_input_not_shadowed_by_priority_bindings(tmp_path):
    """Regression: the browser bindings are priority, so an open modal (New dir /
    Go to) used to lose keys that double as browser actions — `n`, `space`, `o`,
    `a`, `q` etc. fired the underlying action instead of typing into the modal
    Input. check_action() disables every binding while a modal is on the stack."""
    import asyncio

    from textual.widgets import Input

    app = _nav_app(initial=str(tmp_path))

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")            # open the New dir modal
            await pilot.pause()
            assert len(app.screen_stack) > 1  # modal is up
            # every one of these doubles as a browser binding; they must type
            # into the modal Input, not fire add_current / toggle / sort / quit.
            for key in ("a", "space", "o", "q"):
                await pilot.press(key)
            await pilot.pause()
            inp = app.screen.query_one("#dir-input", Input)
            assert inp.value == "a oq"
            # still on the modal — none of those keys confirmed/aborted the app
            assert len(app.screen_stack) > 1
            await pilot.press("escape")        # modal's own Esc closes the modal
            await pilot.pause()
            assert len(app.screen_stack) == 1  # back to the browser, app alive

    asyncio.run(run())


# ── multi-select picker (run_multi_select) ────────────────────────────────────
#
# The flat job picker (requeue / select / restart / crashed, and `join`) used to
# reuse the directory browser, which made filtering modal (you could not select
# while filtering) and leaked directory actions (`a` injected an empty-string
# entry, `→` "entered" a .sh file). MultiSelectApp is the purpose-built picker.
import asyncio


def _ms_app(choices):
    from textual_dirbrowser.browser import MultiSelectApp

    return MultiSelectApp("pick", choices)


async def _settle(pilot):
    await pilot.pause()
    await asyncio.sleep(FILTER_DEBOUNCE + 0.05)
    await pilot.pause()


def _type(pilot, text):
    async def go():
        for ch in text:
            await pilot.press(ch)
    return go()


def test_multiselect_filter_matches_label_not_value():
    # label is what the user sees and types; value is an unrelated path.
    choices = [("alpha-run", "/p/x1.sh"), ("beta-run", "/p/x2.sh")]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _type(pilot, "alpha")
            await _settle(pilot)
            assert [e.value for e in app._matches] == ["/p/x1.sh"]

    asyncio.run(run())


def test_multiselect_toggle_while_filtering():
    """The core fix: space selects the highlighted row *without* leaving the
    filter, so you can filter-then-pick in one flow."""
    choices = [(f"job-{i:03d}", f"/p/job-{i:03d}.sh") for i in range(50)]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _type(pilot, "job-007")
            await _settle(pilot)
            assert len(app._matches) == 1
            await pilot.press("space")
            await pilot.pause()
            assert app._selected_order == ["/p/job-007.sh"]
            # still filtering — query intact, not consumed by the toggle
            assert app._query == "job-007"

    asyncio.run(run())


def test_multiselect_selection_persists_across_query_changes():
    choices = [(f"job-{i:03d}", f"/p/job-{i:03d}.sh") for i in range(50)]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _type(pilot, "job-001")
            await _settle(pilot)
            await pilot.press("space")
            await pilot.pause()
            for _ in range(len("job-001")):  # clear the query
                await pilot.press("backspace")
            await _settle(pilot)
            await _type(pilot, "job-002")
            await _settle(pilot)
            await pilot.press("space")
            await pilot.pause()
            assert app._selected_order == ["/p/job-001.sh", "/p/job-002.sh"]

    asyncio.run(run())


def test_multiselect_enter_confirms_highlighted_when_nothing_toggled():
    choices = [("a", "/p/a.sh"), ("b", "/p/b.sh")]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # nothing toggled -> highlighted row
            await pilot.pause()
        assert app.return_value == ["/p/a.sh"]

    asyncio.run(run())


def test_multiselect_ctrl_a_toggles_all_matches():
    choices = [(f"job-{i:02d}", f"/p/job-{i:02d}.sh") for i in range(10)]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+a")
            await pilot.pause()
            assert len(app._selected_order) == 10
            await pilot.press("ctrl+a")  # all selected -> clear
            await pilot.pause()
            assert app._selected_order == []

    asyncio.run(run())


def test_multiselect_esc_clears_query_then_aborts():
    choices = [("a", "/p/a.sh")]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _type(pilot, "zzz")
            await _settle(pilot)
            await pilot.press("escape")  # query non-empty -> clear, no abort
            await _settle(pilot)
            assert app._query == ""
            assert len(app._matches) == 1
            await pilot.press("escape")  # empty query -> abort
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_multiselect_ctrl_c_aborts_even_mid_query():
    """B19: SIGINT can't reach a raw-mode TUI, so ctrl+c is bound to abort —
    it must cancel outright even while a query is typed (unlike Esc's ladder)."""
    choices = [("a", "/p/a.sh")]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _type(pilot, "a")
            await pilot.press("ctrl+c")
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_multiselect_query_bar_actually_renders():
    """Regression: the query bar must be visible and show the typed query. It
    was once docked on top of the Header (overlapping, hidden) and later sized
    to a single row that its bottom border consumed (content height 0), so the
    query was invisible even though filtering worked."""
    from textual.widgets import Header

    choices = [(f"job-{i:02d}", f"/p/job-{i:02d}.sh") for i in range(20)]

    async def run():
        app = _ms_app(choices)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "job-1")
            await _settle(pilot)
            bar = app.query_one("#query-bar")
            header = app.query_one(Header)
            # not overlapping the header, and tall enough for text + border
            assert bar.region.y >= header.region.y + header.region.height
            assert bar.region.height >= 2
            # the typed query is actually painted on the first row of the bar
            rendered = bar.render_line(0).text
            assert "job-1" in rendered

    asyncio.run(run())


def test_browser_path_bar_renders_current_dir():
    """Regression (B11/B12): #path-bar was docked on top of the Header AND a
    bordered single row, so the current-dir line was invisible."""
    from textual_dirbrowser.browser import BrowserApp, DirEntry
    from tests.tui_helpers import assert_bar_renders

    ents = [DirEntry(label="a", value="/root/mydir/a")]
    app = BrowserApp(
        "Browse", "/root/mydir", "/root",
        lambda _p: (list(ents), 0), lambda p: p,
    )

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert_bar_renders(app, "#path-bar", "mydir")

    asyncio.run(run())


def test_browser_filter_bar_renders_draft_while_editing():
    """Regression (B11): #filter-bar was height:1 + border-bottom, so the `/`
    filter draft was invisible while typing (fetch/stream/video)."""
    from textual_dirbrowser.browser import BrowserApp, DirEntry
    from tests.tui_helpers import assert_bar_renders

    ents = [DirEntry(label=f"d{i}", value=f"/root/d{i}") for i in range(5)]
    app = BrowserApp(
        "Browse", "/root", "/root",
        lambda _p: (list(ents), 0), lambda p: p,
    )

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in "abc":
                await pilot.press(ch)
            await pilot.pause()
            assert_bar_renders(app, "#filter-bar", "abc")

    asyncio.run(run())


def test_multiselect_query_bar_renders_via_helper():
    """The picker query bar (the bar that first exposed B11) stays visible."""
    from tests.tui_helpers import assert_bar_renders

    app = _ms_app([(f"job-{i:02d}", f"/p/{i}.sh") for i in range(10)])

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "job-0")
            await _settle(pilot)
            assert_bar_renders(app, "#query-bar", "job-0")

    asyncio.run(run())


def test_multiselect_render_cap():
    from textual_dirbrowser.browser import RENDER_CAP

    choices = [(f"job-{i:05d}", f"/p/job-{i:05d}.sh") for i in range(RENDER_CAP + 300)]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app._matches) == RENDER_CAP + 300
            assert len(app._entries) == RENDER_CAP

    asyncio.run(run())
# ── start-mode browser (V102–V106) ───────────────────────────────────────────
#
# In start mode the browser is the command's only interactive step: confirming
# IS starting (no destination picker app follows). s/Enter return
# (sources, dest), q joins Esc as cancel, and with nothing toggled the
# highlighted dir becomes the single source. `S` flips the same app into
# destination mode on the local filesystem.

def _start_app(initial="/root", tree=None, dest_initial="/dest/root"):
    from textual_dirbrowser.browser import StartBrowserApp

    tree = tree if tree is not None else {
        "/root": [DirEntry(label="exp1", value="/root/exp1"),
                  DirEntry(label="exp2", value="/root/exp2")],
        "/root/exp1": [],
        "/root/exp2": [],
    }
    dest_tree = {
        dest_initial: [DirEntry(label="out1", value=f"{dest_initial}/out1")],
    }
    return StartBrowserApp(
        "t", initial, "/root",
        list_fn=lambda p: (list(tree.get(p, [])), 0),
        parent_fn=lambda _p: "/root",
        dest_list_fn=lambda p: (list(dest_tree.get(p, [])), 0),
        dest_parent_fn=lambda _p: dest_initial,
        dest_initial=dest_initial,
    )


def test_start_mode_bindings_are_priority():
    from textual.binding import Binding

    from textual_dirbrowser.browser import StartBrowserApp

    bindings = StartBrowserApp.BINDINGS
    assert all(isinstance(b, Binding) for b in bindings)
    assert all(b.priority for b in bindings)
    assert {b.key for b in bindings} == {"enter", "s", "S", "d", "q"}


def test_start_mode_s_returns_selection_default_dest():
    import asyncio

    async def run():
        app = _start_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # toggle exp1
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
        assert app.return_value == (["/root/exp1"], None, None)

    asyncio.run(run())


def test_start_mode_enter_aliases_start():
    import asyncio

    async def run():
        app = _start_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert app.return_value == (["/root/exp1"], None, None)

    asyncio.run(run())


def test_start_mode_q_cancels():
    """q flips from confirm (default mode) to cancel in start mode — a stray q
    must never kick off a transfer/encode."""
    import asyncio

    async def run():
        app = _start_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # even with a selection…
            await pilot.pause()
            await pilot.press("q")       # …q aborts
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_start_mode_empty_selection_uses_highlighted():
    import asyncio

    async def run():
        app = _start_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")    # highlight exp2, nothing toggled
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
        assert app.return_value == (["/root/exp2"], None, None)

    asyncio.run(run())


def test_start_mode_empty_listing_noops():
    import asyncio

    async def run():
        app = _start_app(initial="/root", tree={"/root": []})
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")       # nothing to start on — stays open
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_default_mode_q_cancels():
    """V112: q = quit/cancel in every mode, never confirm; Enter confirms."""
    import asyncio

    async def run():
        app = _nav_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_default_mode_enter_confirms():
    import asyncio

    async def run():
        app = _nav_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert app.return_value == ["/root/exp1"]

    asyncio.run(run())


# ── destination mode (V106) ──────────────────────────────────────────────────

def test_dest_mode_shift_s_flips_to_local_listing(tmp_path):
    import asyncio

    async def run():
        app = _start_app(dest_initial=str(tmp_path / "droot"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # sources = exp1
            await pilot.pause()
            await pilot.press("S")
            await pilot.pause()
            assert app._dest_mode is True
            assert app._pick_current is True
            assert app._pending_sources == ["/root/exp1"]
            # dest_initial created lazily, only on entering dest mode
            assert (tmp_path / "droot").is_dir()
            await pilot.press("q")
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


def test_dest_mode_start_here_returns_current_dir(tmp_path):
    import asyncio

    droot = str(tmp_path / "droot")

    async def run():
        app = _start_app(dest_initial=droot)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("S")
            await pilot.pause()
            await pilot.press("s")       # start HERE (= dest_initial)
            await pilot.pause()
        assert app.return_value == (["/root/exp1"], droot, None)

    asyncio.run(run())


def test_dest_mode_esc_backs_out_to_sources(tmp_path):
    import asyncio

    droot = str(tmp_path / "droot")

    async def run():
        app = _start_app(dest_initial=droot)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("S")
            await pilot.pause()
            assert app._current == droot
            await pilot.press("escape")  # back to sources, not cancel
            await pilot.pause()
            assert app._dest_mode is False
            assert app._current == "/root"
            await pilot.press("s")       # can still start with default dest
            await pilot.pause()
        assert app.return_value == (["/root/exp1"], None, None)

    asyncio.run(run())


def test_dest_mode_uses_highlighted_source_when_none_toggled(tmp_path):
    import asyncio

    droot = str(tmp_path / "droot")

    async def run():
        app = _start_app(dest_initial=droot)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")    # highlight exp2
            await pilot.pause()
            await pilot.press("S")
            await pilot.pause()
            await pilot.press("enter")   # start HERE
            await pilot.pause()
        assert app.return_value == (["/root/exp2"], droot, None)

    asyncio.run(run())


def test_modal_keys_do_not_leak_into_vim_motions(tmp_path):
    """B16/V107: keys typed into the New-dir modal must not reach on_key's
    vim-motion fallthrough — backspace corrections used to walk the browser up
    one directory per press while the modal was open."""
    import asyncio

    tree = {
        "/root": [DirEntry(label="a", value="/root/a")],
        "/root/a": [DirEntry(label="b", value="/root/a/b")],
        "/root/a/b": [],
    }

    from textual_dirbrowser.browser import BrowserApp

    async def run():
        app = BrowserApp(
            "t", "/root/a/b", "/root",
            list_fn=lambda p: (list(tree.get(p, [])), 0),
            parent_fn=lambda p: "/".join(p.split("/")[:-1]) or "/root",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")           # open the New-dir modal
            await pilot.pause()
            assert len(app.screen_stack) > 1
            # motion keys typed as part of a dirname must only edit the input
            for key in ("l", "o", "g", "backspace", "backspace", "j"):
                await pilot.press(key)
            await pilot.pause()
            assert app._current == "/root/a/b"   # browser did not move
            assert len(app.screen_stack) > 1     # modal still open
            await pilot.press("escape")          # modal's own escape works
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(run())


# ── start-in-place (no destination mode — video) ─────────────────────────────

def _start_in_place_app():
    from textual_dirbrowser.browser import StartBrowserApp

    tree = {
        "/root": [DirEntry(label="exp1", value="/root/exp1"),
                  DirEntry(label="exp2", value="/root/exp2")],
        "/root/exp1": [],
        "/root/exp2": [],
    }
    return StartBrowserApp(
        "t", "/root", "/root",
        list_fn=lambda p: (list(tree.get(p, [])), 0),
        parent_fn=lambda _p: "/root",
        # no dest fns → no destination mode
    )


def test_start_in_place_returns_current_dir_as_dest():
    """V106: without dest fns, starting returns the browsed dir as dest."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # select exp1
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert app.return_value == (["/root/exp1"], "/root", None)

    asyncio.run(run())


def test_output_mark_returned_on_start():
    """Starting returns the mark as the third element — the caller uses it as
    the output root, beating even --output-dir."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # select exp1 as source
            await pilot.pause()
            await pilot.press("down")    # highlight exp2
            await pilot.press("d")       # mark exp2 as output
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
        assert app.return_value == (["/root/exp1"], "/root", "/root/exp2")

    asyncio.run(run())


def test_output_mark_refuses_implicit_marked_source():
    """Empty selection + cursor on the marked dir: the highlighted dir would
    become the single source, but it is the output — outputs share the input
    filenames and would clobber them. Starting is refused with a notice."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")       # mark exp1, nothing selected
            await pilot.pause()
            await pilot.press("s")       # refused — browse stays open
            await pilot.pause()
            assert app.return_value is None
            await pilot.press("down")    # highlight exp2 instead
            await pilot.press("s")
            await pilot.pause()
        assert app.return_value == (["/root/exp2"], "/root", "/root/exp1")

    asyncio.run(run())


def test_output_mark_d_marks_highlighted():
    """`d` marks the highlighted dir as the output root, shown as a magenta ◆
    distinct from the green ● source glyph."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")       # mark exp1
            await pilot.pause()
            assert app._output_mark == "/root/exp1"
            assert "◆" in app._row_glyph("/root/exp1")
            assert "magenta" in app._row_glyph("/root/exp1")
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_output_mark_d_toggles_off_and_moves():
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")       # mark exp1
            await pilot.pause()
            await pilot.press("d")       # d again unmarks
            await pilot.pause()
            assert app._output_mark is None
            await pilot.press("d")       # re-mark exp1
            await pilot.press("down")    # highlight exp2
            await pilot.press("d")       # mark moves — single output root
            await pilot.pause()
            assert app._output_mark == "/root/exp2"
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_output_mark_displaces_selection_both_ways():
    """A dir cannot be both a selected source and the output mark — the newer
    act displaces the older."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")   # select exp1
            await pilot.pause()
            await pilot.press("d")       # marking it unselects it
            await pilot.pause()
            assert app._output_mark == "/root/exp1"
            assert app._selected == []
            await pilot.press("space")   # selecting the mark clears the mark
            await pilot.pause()
            assert app._output_mark is None
            assert app._selected == ["/root/exp1"]
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_output_mark_hidden_with_dest_mode():
    """`d` exists only in browsers without destination mode (video); in fetch
    the key must stay inert."""
    import asyncio

    async def run():
        app = _start_app()               # has dest fns → destination mode
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert app._output_mark is None
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_output_mark_panel_shows_output_root():
    """Right panel: marked path when marked, dim start-in-place hint when not."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            extra = app._selection_extra_lines()
            assert any("start-in-place" in line for line in extra)
            await pilot.press("d")
            await pilot.pause()
            extra = app._selection_extra_lines()
            assert any("/root/exp1" in line and "◆" in line for line in extra)
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_start_in_place_shift_s_is_inert():
    """Without dest fns, S must neither flip modes nor exit."""
    import asyncio

    async def run():
        app = _start_in_place_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("S")
            await pilot.pause()
            assert app._dest_mode is False
            assert app._current == "/root"
            await pilot.press("escape")
            await pilot.pause()
        assert app.return_value is None

    asyncio.run(run())


# ── leaf (file) entries: selectable, not navigable ────────────────────────────
#
# A DirEntry with is_dir=False marks a leaf (e.g. a file): it can be
# Space-selected but never navigated into, and can't become the `d` output mark.
# Defaults keep every existing (all-directory) caller unchanged.

def _leaf_app(initial="/root"):
    from textual_dirbrowser.browser import BrowserApp

    tree = {
        "/root": [
            DirEntry(label="exp1", value="/root/exp1"),
            DirEntry(label="clip.mp4", value="/root/clip.mp4", is_dir=False),
        ],
        "/root/exp1": [DirEntry(label="sub", value="/root/exp1/sub")],
    }
    return BrowserApp(
        "t", initial, "/root",
        list_fn=lambda p: (list(tree.get(p, [])), 0),
        parent_fn=lambda _p: "/root",
    )


def test_dir_entry_is_dir_defaults_true():
    # Existing callers construct DirEntry(label, value) with no is_dir — those
    # entries must keep directory (navigable) behaviour.
    assert DirEntry(label="x", value="/root/x").is_dir is True


def test_leaf_entry_right_arrow_does_not_navigate():
    """→/l on a leaf (file) entry is a no-op; only directories navigate."""
    import asyncio

    async def run():
        app = _leaf_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current == "/root"
            await pilot.press("down")    # move to clip.mp4 (leaf, index 1)
            await pilot.press("right")   # would enter — but a leaf can't
            await pilot.pause()
        assert app._current == "/root"   # unchanged, no crash

    asyncio.run(run())


def test_leaf_entry_space_selects():
    """Space marks a leaf just like a directory (value-based selection)."""
    import asyncio

    async def run():
        app = _leaf_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")    # highlight clip.mp4
            await pilot.press("space")   # select it
            await pilot.pause()
        assert app._selected == ["/root/clip.mp4"]

    asyncio.run(run())


def test_dir_entry_right_arrow_still_navigates():
    """Guard is leaf-specific: a directory entry still enters on →."""
    import asyncio

    async def run():
        app = _leaf_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("right")   # highlighted exp1 (dir) — navigates
            await pilot.pause()
        assert app._current == "/root/exp1"

    asyncio.run(run())


def test_output_mark_refuses_leaf():
    """`d` can't mark a leaf (file) as the output root — output must be a dir."""
    import asyncio

    async def run():
        from textual_dirbrowser.browser import StartBrowserApp

        tree = {
            "/root": [
                DirEntry(label="exp1", value="/root/exp1"),
                DirEntry(label="clip.mp4", value="/root/clip.mp4", is_dir=False),
            ],
            "/root/exp1": [],
        }
        app = StartBrowserApp(
            "t", "/root", "/root",
            list_fn=lambda p: (list(tree.get(p, [])), 0),
            parent_fn=lambda _p: "/root",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")    # highlight clip.mp4 (leaf)
            await pilot.press("d")       # attempt to mark — refused
            await pilot.pause()
            assert app._output_mark is None

    asyncio.run(run())


def test_multiselect_no_auto_advance_keeps_cursor():
    from textual_dirbrowser.browser import MultiSelectApp

    choices = [(f"job-{i}", f"/p/{i}.sh") for i in range(5)]

    async def run():
        app = MultiSelectApp("pick", choices, auto_advance=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#nav")
            assert lv.index == 0
            await pilot.press("space")
            await pilot.pause()
            assert lv.index == 0  # no jump
            assert app._selected_order == ["/p/0.sh"]

    asyncio.run(run())


def test_multiselect_auto_advance_default_still_jumps():
    choices = [(f"job-{i}", f"/p/{i}.sh") for i in range(5)]

    async def run():
        app = _ms_app(choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            assert app.query_one("#nav").index == 1

    asyncio.run(run())


def test_multiselect_closed_filter_navigates_with_jk():
    from textual_dirbrowser.browser import MultiSelectApp

    choices = [(f"job-{i}", f"/p/{i}.sh") for i in range(5)]

    async def run():
        app = MultiSelectApp("pick", choices, live_filter=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.press("j")
            await pilot.pause()
            assert app.query_one("#nav").index == 2
            assert app._query == ""  # j did not type into the query

    asyncio.run(run())


def test_multiselect_slash_opens_filter_enter_accepts():
    from textual_dirbrowser.browser import MultiSelectApp

    choices = [("alpha", "/p/a.sh"), ("beta", "/p/b.sh")]

    async def run():
        app = MultiSelectApp("pick", choices, live_filter=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert app._query_editing is True
            await _type(pilot, "beta")
            await _settle(pilot)
            assert [e.value for e in app._matches] == ["/p/b.sh"]
            await pilot.press("enter")
            await pilot.pause()
            # enter accepted the query (no confirm/exit), still filtered
            assert app._query_editing is False
            assert [e.value for e in app._matches] == ["/p/b.sh"]
            assert app.return_value is None  # app still running (not exited)

    asyncio.run(run())


def test_multiselect_esc_closes_and_clears_filter():
    from textual_dirbrowser.browser import MultiSelectApp

    choices = [("alpha", "/p/a.sh"), ("beta", "/p/b.sh")]

    async def run():
        app = MultiSelectApp("pick", choices, live_filter=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await _type(pilot, "beta")
            await _settle(pilot)
            await pilot.press("escape")
            await pilot.pause()
            assert app._query_editing is False
            assert app._query == ""
            assert len(app._matches) == 2

    asyncio.run(run())

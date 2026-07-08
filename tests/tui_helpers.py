"""Shared test helper: assert a Textual bar actually renders its text.

Headless Pilot tests verify logic but a present-yet-invisible widget passes
them — a 1-row bar whose border consumes its only content row reserves space
yet paints nothing (B10/B11). This helper locks V90: a bordered bar must be at
least 2 rows tall AND its text must appear in the rendered strip.
"""
from __future__ import annotations


def assert_bar_renders(app, selector: str, expected: str) -> None:
    """Fail unless ``selector`` is tall enough to show text and actually paints
    ``expected``. Scans every row of the widget's region because the content row
    sits above a ``border-bottom`` but below a ``border-top``."""
    widget = app.query_one(selector)
    height = widget.region.height
    assert height >= 2, (
        f"{selector}: region height {height} < 2 — a bordered single-row bar "
        f"hides its text (border eats the only content row)"
    )
    rendered = "\n".join(widget.render_line(y).text for y in range(height))
    assert expected in rendered, (
        f"{selector}: did not render {expected!r}; got {rendered!r}"
    )

"""Default Textual styling for the browser apps.

Hosts that want a different look subclass the app classes and override
``DEFAULT_CSS`` (they are public for exactly that reason).
"""

TUI_CSS = """
Screen {
    background: $surface;
    color: $foreground;
}

Header {
    background: $panel;
    color: $foreground;
    text-style: bold;
}

Footer {
    background: $panel;
    color: $text-muted;
}

Input {
    background: $surface;
    border: tall $panel;
    color: $foreground;
    padding: 0 1;
}

Input:focus {
    border: tall $accent;
}

ListView,
OptionList {
    background: $surface;
    border: none;
}

ListView > ListItem {
    padding: 0 2;
    color: $foreground;
}

ListView > ListItem.--highlight {
    background: $accent 25%;
    color: $foreground;
    text-style: bold;
}

OptionList > .option {
    padding: 0 2;
}

OptionList > .option-highlighted {
    background: $accent 25%;
    color: $foreground;
    text-style: bold;
}

.tui-bar {
    height: 1;
    background: $panel;
    color: $text-muted;
    padding: 0 2;
}

.tui-title {
    color: $accent;
    text-style: bold;
}
"""


MODAL_CSS = TUI_CSS + """
ModalScreen {
    background: $background 40%;
}

#dialog {
    width: 56;
    height: auto;
    padding: 1 2;
    background: $surface;
    border: round $accent;
}

#dialog Label {
    margin-bottom: 1;
    color: $accent;
    text-style: bold;
}
"""

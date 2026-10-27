"""Display-free stand-ins for the few GTK widgets the tray builds directly.

A real Gtk.Label needs a display connection (CI has none: GTK aborts with
"Can't create a GtkStyleContext without a display connection"). FakeLabel
records exactly what the tray sets, so tests still check the real code.
"""

from __future__ import annotations


class FakeLabel:
    def __init__(self, label: str = "") -> None:
        self._text = label
        self._markup = False
        self._wrap = False
        self._wrap_mode = None
        self._width_chars = -1
        self._max_width_chars = -1
        self._xalign = 0.5
        self._halign = None

    def set_text(self, text: str) -> None:
        self._text, self._markup = text, False

    def set_markup(self, markup: str) -> None:
        self._text, self._markup = markup, True

    def get_text(self) -> str:
        return self._text

    def get_use_markup(self) -> bool:
        return self._markup

    def set_line_wrap(self, wrap: bool) -> None:
        self._wrap = wrap

    def get_line_wrap(self) -> bool:
        return self._wrap

    def set_line_wrap_mode(self, mode) -> None:
        self._wrap_mode = mode

    def set_width_chars(self, chars: int) -> None:
        self._width_chars = chars

    def get_width_chars(self) -> int:
        return self._width_chars

    def set_max_width_chars(self, chars: int) -> None:
        self._max_width_chars = chars

    def get_max_width_chars(self) -> int:
        return self._max_width_chars

    def set_xalign(self, value: float) -> None:
        self._xalign = value

    def set_halign(self, value) -> None:
        self._halign = value

"""Terminal styling: colours, sanitising and panels. Standard library only."""

import os
import re
import shutil
import sys
import unicodedata

_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SGR = re.compile(r"\x1b\[[0-9;]*m")


LOGO = "◈"


def clean(text):
    """Remove terminal control sequences. Model replies and tool output are untrusted."""
    return _CONTROL.sub("", _ESCAPES.sub("", str(text)))


class Theme:
    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35",
             "cyan": "36", "accent": "38;5;141", "muted": "38;5;245", "add": "38;5;114", "del": "38;5;210",
             "add_bg": "48;5;22", "del_bg": "48;5;52"}

    def __init__(self, stream=None, color=None):
        self.stream = stream or sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.color = self.tty and os.environ.get("NO_COLOR") is None if color is None else color
        encoding = (getattr(self.stream, "encoding", None) or "").lower()
        self.unicode = "utf" in encoding or not encoding

    def paint(self, text, *styles):
        if not self.color or not styles:
            return text
        codes = ";".join(self.CODES.get(style, style) for style in styles)
        return f"\033[{codes}m{text}\033[0m"

    def width(self):
        return max(40, min(shutil.get_terminal_size((100, 24)).columns, 110))

    def glyph(self, fancy, plain):
        return fancy if self.unicode else plain


def visible_len(text):
    plain = _SGR.sub("", text)
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)


def fit(text, width):
    """Cut plain text to width display columns."""
    if visible_len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def panel(theme, title, lines, style="muted"):
    """Render lines inside a rounded box. Long lines are cut, styled lines are kept whole."""
    width = theme.width()
    tl, tr, bl, br, h, v = (("╭", "╮", "╰", "╯", "─", "│") if theme.unicode else ("+", "+", "+", "+", "-", "|"))
    head = f"{h} {title} " if title else ""
    top = tl + head + h * max(0, width - 2 - visible_len(head)) + tr
    rendered = [theme.paint(top, style)]
    for line in lines:
        room = width - 4
        pad = max(0, room - visible_len(line))
        rendered.append(f"{theme.paint(v, style)} {line}{' ' * pad} {theme.paint(v, style)}")
    rendered.append(theme.paint(bl + h * (width - 2) + br, style))
    return "\n".join(rendered)

"""ANSI styling and width-correct text layout.

Colour is semantic (`ok`, `warn`, `dim`, `accent`…) so every face shares one
palette, and everything degrades to plain text when stdout is not a TTY or
NO_COLOR is set.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from typing import List

ESC = "\x1b"
CSI = ESC + "["
RESET = CSI + "0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")

PALETTE = {
    "bold": "1", "dim": "2", "italic": "3", "under": "4", "reverse": "7",
    "green": "38;5;121", "green2": "38;5;78", "cyan": "38;5;117", "amber": "38;5;221",
    "magenta": "38;5;213", "red": "38;5;203", "white": "38;5;255", "gray": "38;5;245", "dark": "38;5;239",
    "blue": "38;5;111",
    # semantic
    "ok": "38;5;121", "warn": "38;5;221", "bad": "38;5;203", "accent": "38;5;117", "muted": "38;5;245",
    "title": "1;38;5;117", "path": "38;5;111", "prompt": "1;38;5;121",
}


def enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def style(text: str, *names: str, force: bool = False) -> str:
    if not (force or enabled()):
        return text
    codes = ";".join(PALETTE.get(n, n) for n in names if n)
    return f"{CSI}{codes}m{text}{RESET}" if codes else text


def code(*names: str) -> str:
    return CSI + ";".join(PALETTE.get(n, n) for n in names) + "m"


def strip(text: str) -> str:
    return ANSI_RE.sub("", text)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    return sum(char_width(c) for c in strip(text))


def fit(text: str, cols: int, ellipsis: str = "…") -> str:
    """Truncate to `cols` visible columns, preserving ANSI codes; pad to exactly cols."""
    if cols <= 0:
        return ""
    out, used, i, cut = [], 0, 0, False
    while i < len(text):
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        w = char_width(text[i])
        if used + w > cols:
            cut = True
            break
        out.append(text[i])
        used += w
        i += 1
    if cut and ellipsis and cols >= 1:
        # step back to make room for the ellipsis
        while out and used + 1 > cols:
            last = out.pop()
            if not ANSI_RE.fullmatch(last):
                used -= char_width(last)
        out.append(ellipsis)
        used += 1
    s = "".join(out)
    if ANSI_RE.search(s):
        s += RESET
    return s + " " * max(0, cols - used)


def wrap(text: str, cols: int) -> List[str]:
    cols = max(8, cols)
    lines: List[str] = []
    for para in str(text).split("\n"):
        if not para.strip():
            lines.append("")
            continue
        indent = len(para) - len(para.lstrip(" "))
        words = para.split()
        current = " " * indent
        for word in words:
            candidate = (current + " " + word) if current.strip() else current + word
            if width(candidate) <= cols:
                current = candidate
            else:
                if current.strip():
                    lines.append(current)
                while width(word) > cols:
                    lines.append(word[:cols])
                    word = word[cols:]
                current = " " * indent + word
        lines.append(current)
    return lines


def term_size(default=(100, 30)):
    s = shutil.get_terminal_size(default)
    return s.columns, s.lines


def human(n) -> str:
    from ..system import human as h
    return h(n)

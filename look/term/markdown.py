"""Streaming Markdown for the terminal.

Tokens arrive in fragments; complete lines are styled and printed as they
finish, the unfinished tail waits. Code fences, headings, lists, quotes,
**bold**, *italic*, `code` and [links](https://…) (OSC-8 clickable) are handled.
"""
from __future__ import annotations

import re
import sys
from typing import List

from .ansi import enabled, style, wrap, term_size

LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")
ITAL = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
CODE = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
    if not enabled():
        return LINK.sub(lambda m: f"{m.group(1)} <{m.group(2)}>", text)
    slots: List[str] = []

    def keep(s: str) -> str:
        slots.append(s)
        return f"\x00{len(slots) - 1}\x00"

    text = CODE.sub(lambda m: keep(style(m.group(1), "amber")), text)
    text = LINK.sub(lambda m: keep(f"\x1b]8;;{m.group(2)}\x07{style(m.group(1), 'under', 'blue')}\x1b]8;;\x07"), text)
    text = BOLD.sub(lambda m: style(m.group(1), "bold"), text)
    text = ITAL.sub(lambda m: style(m.group(1), "italic"), text)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)


class Stream:
    def __init__(self, out=None, indent: str = ""):
        self.out = out or sys.stdout
        self.buf = ""
        self.in_code = False
        self.indent = indent
        self.printed = False

    def feed(self, text: str) -> None:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._line(line)

    def finish(self) -> None:
        if self.buf:
            self._line(self.buf)
            self.buf = ""
        self.out.flush()

    def _line(self, line: str) -> None:
        cols = term_size()[0] - len(self.indent) - 1
        stripped = line.strip()
        if stripped.startswith("```"):
            self.in_code = not self.in_code
            lang = stripped[3:].strip()
            self._emit(style(("┌ " + lang) if self.in_code and lang else ("┌" if self.in_code else "└"), "dark"))
            return
        if self.in_code:
            self._emit(style("│ ", "dark") + style(line, "white"))
            return
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            self._emit(style(m.group(2), "title" if level <= 2 else "bold"))
            return
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            self._emit(style("─" * min(cols, 60), "dark"))
            return
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)", line)
        if m:
            bullet = "•" if m.group(2) in "-*+" else m.group(2)
            pad = m.group(1)
            rows = wrap(m.group(3), cols - len(pad) - 2)
            for i, row in enumerate(rows):
                lead = (pad + style(bullet, "accent") + " ") if i == 0 else pad + " " * (len(bullet) + 1)
                self._emit(lead + inline(row))
            return
        if stripped.startswith(">"):
            for row in wrap(stripped.lstrip("> "), cols - 2):
                self._emit(style("▎ ", "dark") + style(inline(row), "italic"))
            return
        if not stripped:
            self._emit("")
            return
        for row in wrap(line, cols):
            self._emit(inline(row))

    def _emit(self, text: str) -> None:
        self.out.write(self.indent + text + "\n")
        self.printed = True
        self.out.flush()


def render(text: str) -> str:
    import io
    buf = io.StringIO()
    s = Stream(buf)
    s.feed(text)
    s.finish()
    return buf.getvalue()

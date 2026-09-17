"""Raw keyboard input with proper escape-sequence decoding.

Keys come back as names: "up", "down", "left", "right", "enter", "esc", "tab",
"backspace", "delete", "home", "end", "pgup", "pgdn", "ctrl-c", "ctrl-d" …, or
the literal character.
"""
from __future__ import annotations

import contextlib
import os
import select
import sys
import termios
import tty
from typing import Iterator, Optional

SEQUENCES = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[H": "home", "[F": "end", "OH": "home", "OF": "end", "[1~": "home", "[4~": "end", "[3~": "delete",
    "[5~": "pgup", "[6~": "pgdn", "[Z": "shift-tab", "[2~": "insert",
}


@contextlib.contextmanager
def raw(fd: Optional[int] = None) -> Iterator[int]:
    fd = sys.stdin.fileno() if fd is None else fd
    if not os.isatty(fd):
        yield fd
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)  # we handle ctrl-c ourselves
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read(fd: int, timeout: Optional[float]) -> str:
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return ""
    first = os.read(fd, 1)
    if not first:
        return ""
    lead = first[0]
    extra = 3 if lead >= 0xF0 else 2 if lead >= 0xE0 else 1 if lead >= 0xC0 else 0
    data = first
    for _ in range(extra):
        if select.select([fd], [], [], 0.05)[0]:
            data += os.read(fd, 1)
    return data.decode("utf-8", errors="ignore")


def read_key(fd: Optional[int] = None, timeout: Optional[float] = None) -> Optional[str]:
    fd = sys.stdin.fileno() if fd is None else fd
    ch = _read(fd, timeout)
    if not ch:
        return None
    if ch == "\x1b":
        nxt = _read(fd, 0.03)
        if not nxt:
            return "esc"
        seq = nxt
        if nxt in "[O":
            while True:
                c = _read(fd, 0.03)
                if not c:
                    break
                seq += c
                if c.isalpha() or c == "~":
                    break
        # bracketed paste start: swallow until end marker, return as text
        if seq == "[200~":
            buf = ""
            while not buf.endswith("\x1b[201~"):
                c = _read(fd, 0.5)
                if not c:
                    break
                buf += c
            return "paste:" + buf[:-6] if buf.endswith("\x1b[201~") else "paste:" + buf
        return SEQUENCES.get(seq, "alt-" + seq if len(seq) == 1 else "unknown")
    code = ord(ch)
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\t":
        return "tab"
    if code in (127, 8):
        return "backspace"
    if code < 32:
        return "ctrl-" + chr(code + 96)
    # multibyte utf-8: read continuation bytes
    raw_bytes = ch.encode("utf-8", errors="ignore")
    return ch if raw_bytes else None


class LineEditor:
    """A one-line input field for TUIs (no readline): cursor, history, paste."""

    def __init__(self, text: str = "", history=None):
        self.text = text
        self.cursor = len(text)
        self.history = list(history or [])
        self.hpos = len(self.history)

    def feed(self, key: str) -> Optional[str]:
        """Returns 'submit', 'cancel', or None."""
        if key == "enter":
            if self.text.strip():
                self.history.append(self.text)
            return "submit"
        if key in ("esc", "ctrl-c"):
            return "cancel"
        if key == "backspace" and self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1
        elif key == "delete":
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in ("home", "ctrl-a"):
            self.cursor = 0
        elif key in ("end", "ctrl-e"):
            self.cursor = len(self.text)
        elif key == "ctrl-u":
            self.text, self.cursor = self.text[self.cursor:], 0
        elif key == "ctrl-w":
            head = self.text[:self.cursor].rstrip()
            cut = head.rfind(" ") + 1
            self.text, self.cursor = self.text[:cut] + self.text[self.cursor:], cut
        elif key == "up" and self.history:
            self.hpos = max(0, self.hpos - 1)
            self.text = self.history[self.hpos]
            self.cursor = len(self.text)
        elif key == "down" and self.history:
            self.hpos = min(len(self.history), self.hpos + 1)
            self.text = self.history[self.hpos] if self.hpos < len(self.history) else ""
            self.cursor = len(self.text)
        elif key.startswith("paste:"):
            chunk = key[6:].replace("\r", "\n")
            self.text = self.text[:self.cursor] + chunk + self.text[self.cursor:]
            self.cursor += len(chunk)
        elif len(key) == 1 and key.isprintable():
            self.text = self.text[:self.cursor] + key + self.text[self.cursor:]
            self.cursor += 1
        return None

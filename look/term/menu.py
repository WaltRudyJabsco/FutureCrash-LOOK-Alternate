"""A small, reusable picker: filterable list with an optional detail column.

Used by `lk commands`, `lk models`, `lk settings`, `lk personality` — one widget,
so every chooser in LOOK feels the same.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from . import keys
from .ansi import RESET, code, fit, style, term_size, width

ALT_ON, ALT_OFF, HIDE, SHOW = "\x1b[?1049h", "\x1b[?1049l", "\x1b[?25l", "\x1b[?25h"


@dataclass
class Item:
    label: str
    detail: str = ""
    value: object = None
    current: bool = False
    group: str = ""


def pick(title: str, items: Sequence[Item], hint: str = "", filterable: bool = True,
         on_key: Optional[Callable[[str, Item], Optional[str]]] = None, start: int = 0) -> Optional[Item]:
    """Returns the chosen Item, or None when cancelled. `on_key(key, item)` may handle
    extra keys; returning "refresh" redraws, "done" returns the item, anything else continues."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        for it in items:
            print(f"{'*' if it.current else ' '} {it.label}\t{it.detail}")
        return None
    query = ""
    index = next((i for i, it in enumerate(items) if it.current), start)
    top = 0
    sys.stdout.write(ALT_ON + HIDE)
    try:
        with keys.raw():
            while True:
                visible = [it for it in items if all(w in (it.label + " " + it.detail + " " + it.group).casefold()
                                                      for w in query.casefold().split())]
                index = max(0, min(index, len(visible) - 1))
                cols, rows = term_size()
                cols -= 1
                body_h = rows - 4
                if index < top:
                    top = index
                if index >= top + body_h:
                    top = index - body_h + 1
                label_w = min(max((width(it.label) for it in items), default=10) + 2, cols // 2)
                out = [fit(style(f" {title} ", "reverse", "accent") + "  " + style(f"{len(visible)}/{len(items)}", "muted"), cols),
                       fit((style(" filter › ", "accent") + query + style("▏", "accent")) if query else style(" type to filter", "dark"), cols)]
                for i in range(top, min(len(visible), top + body_h)):
                    it = visible[i]
                    mark = style("●", "ok") if it.current else " "
                    group = style(f"{it.group:<8}", "dark") if it.group else ""
                    line = f" {mark} {group}{fit(it.label, label_w)}{style(it.detail, 'muted')}"
                    out.append(code("reverse") + fit(line.replace(RESET, RESET + code("reverse")), cols) + RESET if i == index else fit(line, cols))
                out += [""] * (rows - 1 - len(out))
                out.append(fit(style(hint or "↑↓ move · enter choose · esc cancel", "dark"), cols))
                sys.stdout.write("\x1b[H" + "\n".join(o + "\x1b[K" for o in out[:rows]))
                sys.stdout.flush()
                key = keys.read_key()
                if key is None:
                    continue
                if key in ("esc", "ctrl-c"):
                    if query:
                        query = ""
                        continue
                    return None
                if key == "enter" and visible:
                    return visible[index]
                if key in ("up", "ctrl-p"):
                    index -= 1
                elif key in ("down", "ctrl-n"):
                    index += 1
                elif key == "pgup":
                    index -= body_h
                elif key == "pgdn":
                    index += body_h
                elif on_key and visible and (key in ("left", "right", "tab", "delete") or (len(key) == 1 and not filterable)):
                    if on_key(key, visible[index]) == "done":
                        return visible[index]
                elif key == "backspace":
                    query = query[:-1]
                elif filterable and len(key) == 1 and key.isprintable():
                    query += key
                    index = 0
    finally:
        sys.stdout.write(SHOW + ALT_OFF)
        sys.stdout.flush()

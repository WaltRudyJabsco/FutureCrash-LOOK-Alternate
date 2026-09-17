"""The Signal canvas: a 40×12 model-drawable framebuffer.

The Oracle draws with a tiny line-oriented language. In 4.0 drawing is a real
tool (`signal`), so a drawing produces a structured receipt the model sees on
its next round — its craft feedback loop — and the host never has to fish
[[SIGNAL]] blocks out of prose. The text-block form is still accepted from
models that cannot call tools.

    CLEAR                               TITLE words…
    TTL seconds (5–180, 0 = persist)    FPS n (1–12)
    PUT x y color ch                    TEXT x y color words…
    LINE x1 y1 x2 y2 color ch           ARROW x1 y1 x2 y2 color ch
    BOX x y w h color ch                FILL x y w h color ch
    CIRCLE cx cy r color ch             ELLIPSE cx cy rx ry color ch
    PLOT color ch v1 v2 …               BARS x y color v1 v2 …
    SPRITE x y color                    FRAME   (starts an animation frame; ≤8)
      …rows…
    END
colors: green cyan amber magenta red white dim
"""
from __future__ import annotations

import math
import re
import time
from typing import Dict, List, Optional, Tuple

from ... import store
from ...paths import roots

WIDTH, HEIGHT = 40, 12
COLORS = ("green", "cyan", "amber", "magenta", "red", "white", "dim")
LANGUAGE = """CLEAR | TITLE words | TTL seconds (0 = persist) | FPS n
PUT x y color ch | TEXT x y color words
LINE x1 y1 x2 y2 color ch | ARROW x1 y1 x2 y2 color ch
BOX x y w h color ch | FILL x y w h color ch
CIRCLE cx cy r color ch | ELLIPSE cx cy rx ry color ch
PLOT color ch v1 v2 ... | BARS x y color v1 v2 ...
SPRITE x y color, then raw rows, then END
FRAME starts an animation frame (up to 8)
Grid is 40 wide x 12 tall, origin top-left. Colors: green cyan amber magenta red white dim."""
BLOCK = re.compile(r"\[\[SIGNAL\]\](.*?)\[\[/SIGNAL\]\]", re.S | re.I)
HISTORY = 6

Cell = Optional[Tuple[str, str]]


def blank() -> List[List[Cell]]:
    return [[None] * WIDTH for _ in range(HEIGHT)]


class Canvas:
    def __init__(self):
        self.cells = blank()
        self.frames: List[List[List[Cell]]] = []
        self.fps = 4.0
        self.title = ""
        self.expires = 0.0     # 0 = persists until cleared
        self.started = time.time()
        self.updated = 0.0
        self.receipts: List[str] = []
        self._stats = {"accepted": 0, "rejected": 0, "clipped": 0, "modes": set()}

    # ---- primitives ------------------------------------------------------
    def put(self, x: int, y: int, ch: str, color: str) -> None:
        if not ch:
            return
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            self.cells[y][x] = (ch[0], color if color in COLORS else "cyan")
        else:
            self._stats["clipped"] += 1

    def text(self, x: int, y: int, color: str, words: str) -> None:
        for i, ch in enumerate(words):
            if ch != " ":
                self.put(x + i, y, ch, color)

    def line(self, x1: int, y1: int, x2: int, y2: int, color: str, ch: str) -> None:
        dx, dy = abs(x2 - x1), -abs(y2 - y1)
        sx, sy = (1 if x1 < x2 else -1), (1 if y1 < y2 else -1)
        err = dx + dy
        for _ in range(200):
            self.put(x1, y1, ch, color)
            if x1 == x2 and y1 == y2:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x1 += sx
            if e2 <= dx:
                err += dx
                y1 += sy

    def box(self, x: int, y: int, w: int, h: int, color: str, ch: str) -> None:
        if w <= 0 or h <= 0:
            return
        self.line(x, y, x + w - 1, y, color, ch)
        self.line(x, y + h - 1, x + w - 1, y + h - 1, color, ch)
        self.line(x, y, x, y + h - 1, color, ch)
        self.line(x + w - 1, y, x + w - 1, y + h - 1, color, ch)

    def fill(self, x: int, y: int, w: int, h: int, color: str, ch: str) -> None:
        for yy in range(y, y + max(0, h)):
            for xx in range(x, x + max(0, w)):
                self.put(xx, yy, ch, color)

    def ellipse(self, cx: int, cy: int, rx: int, ry: int, color: str, ch: str) -> None:
        steps = max(24, 8 * max(rx, ry))
        for i in range(steps):
            a = 2 * math.pi * i / steps
            self.put(round(cx + rx * math.cos(a)), round(cy + ry * math.sin(a)), ch, color)

    def arrow(self, x1: int, y1: int, x2: int, y2: int, color: str, ch: str) -> None:
        self.line(x1, y1, x2, y2, color, ch)
        head = ">" if abs(x2 - x1) >= abs(y2 - y1) and x2 >= x1 else "<" if abs(x2 - x1) >= abs(y2 - y1) else "v" if y2 > y1 else "^"
        self.put(x2, y2, head, color)

    def plot(self, color: str, ch: str, values: List[float]) -> None:
        if not values:
            return
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        prev = None
        for i, v in enumerate(values):
            x = round(i * (WIDTH - 1) / max(1, len(values) - 1))
            y = round((HEIGHT - 1) - (v - lo) / span * (HEIGHT - 1))
            if prev:
                self.line(prev[0], prev[1], x, y, color, ch)
            prev = (x, y)

    def bars(self, x: int, y: int, color: str, values: List[float]) -> None:
        if not values:
            return
        hi = max(values) or 1.0
        for i, v in enumerate(values):
            height = max(0, round(v / hi * (y + 1)))
            for k in range(height):
                self.put(x + i * 2, y - k, "█", color)

    def sprite(self, x: int, y: int, color: str, rows: List[str]) -> None:
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch != " ":
                    self.put(x + dx, y + dy, ch, color)

    # ---- language --------------------------------------------------------
    def _command(self, line: str, ttl: List[float]) -> None:
        parts = line.split()
        cmd = parts[0].upper()
        c = lambda s: s.lower()
        try:
            if cmd == "CLEAR":
                self.cells = blank()
            elif cmd == "TITLE" and len(parts) >= 2:
                self.title = " ".join(parts[1:])[:28]
            elif cmd == "TTL" and len(parts) >= 2:
                v = float(parts[1])
                ttl[0] = 0.0 if v <= 0 else max(5.0, min(180.0, v))
            elif cmd == "FPS" and len(parts) >= 2:
                self.fps = max(1.0, min(12.0, float(parts[1])))
            elif cmd == "PUT" and len(parts) >= 5:
                self.put(int(parts[1]), int(parts[2]), parts[4], c(parts[3])); self._stats["modes"].add("raster")
            elif cmd == "TEXT" and len(parts) >= 5:
                self.text(int(parts[1]), int(parts[2]), c(parts[3]), line.split(None, 4)[4]); self._stats["modes"].add("vector")
            elif cmd in {"LINE", "BOX", "FILL", "ARROW"} and len(parts) >= 7:
                fn = {"LINE": self.line, "BOX": self.box, "FILL": self.fill, "ARROW": self.arrow}[cmd]
                fn(*map(int, parts[1:5]), c(parts[5]), parts[6]); self._stats["modes"].add("vector")
            elif cmd == "CIRCLE" and len(parts) >= 6:
                r = int(parts[3])
                self.ellipse(int(parts[1]), int(parts[2]), r * 2, r, c(parts[4]), parts[5]); self._stats["modes"].add("semantic")
            elif cmd == "ELLIPSE" and len(parts) >= 7:
                self.ellipse(*map(int, parts[1:5]), c(parts[5]), parts[6]); self._stats["modes"].add("semantic")
            elif cmd == "PLOT" and len(parts) >= 4:
                self.plot(c(parts[1]), parts[2], [float(v) for v in parts[3:]]); self._stats["modes"].add("semantic")
            elif cmd == "BARS" and len(parts) >= 5:
                self.bars(int(parts[1]), int(parts[2]), c(parts[3]), [float(v) for v in parts[4:]]); self._stats["modes"].add("semantic")
            else:
                self._stats["rejected"] += 1
                return
            self._stats["accepted"] += 1
        except (ValueError, IndexError):
            self._stats["rejected"] += 1

    def _items(self, program: str):
        lines = program.splitlines()
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if not s or s.startswith("#"):
                i += 1
                continue
            if s.upper().startswith("SPRITE "):
                rows, i, closed = [], i + 1, False
                while i < len(lines):
                    if lines[i].strip().upper() == "END":
                        closed, i = True, i + 1
                        break
                    rows.append(lines[i].rstrip("\r"))
                    i += 1
                yield ("sprite", s, rows, closed)
                continue
            yield ("line", s, None, True)
            i += 1

    def _apply(self, item, ttl) -> None:
        kind, text, rows, closed = item
        if kind == "sprite":
            parts = text.split()
            if not closed or len(parts) < 4:
                self._stats["rejected"] += 1
                return
            try:
                self.sprite(int(parts[1]), int(parts[2]), parts[3].lower(), rows)
                self._stats["accepted"] += 1
                self._stats["modes"].add("raster")
            except ValueError:
                self._stats["rejected"] += 1
        else:
            self._command(text, ttl)

    def run(self, program: str, default_ttl: float = 0.0) -> Dict[str, object]:
        """Execute a drawing program. Returns the receipt as data."""
        self._stats = {"accepted": 0, "rejected": 0, "clipped": 0, "modes": set()}
        ttl = [default_ttl]
        items = list(self._items(program))
        if any(k == "line" and t.upper() == "FRAME" for k, t, _, _ in items):
            head, sections, current = [], [], None
            for it in items:
                if it[0] == "line" and it[1].upper() == "FRAME":
                    if current is not None:
                        sections.append(current)
                    current = []
                elif current is None:
                    head.append(it)
                else:
                    current.append(it)
            if current is not None:
                sections.append(current)
            for it in head:
                self._apply(it, ttl)
            title = self.title
            self.frames = []
            for sec in sections[:8]:
                self.cells, self.title = blank(), title
                for it in sec:
                    self._apply(it, ttl)
                self.frames.append([row[:] for row in self.cells])
        else:
            self.frames = []
            for it in items:
                self._apply(it, ttl)
        self.started = time.time()
        self.updated = time.time()
        self.expires = 0.0 if ttl[0] <= 0 else time.time() + ttl[0]
        return self._receipt()

    def _receipt(self) -> Dict[str, object]:
        cells = self.frames[-1] if self.frames else self.cells
        pts = [(x, y) for y, row in enumerate(cells) for x, v in enumerate(row) if v is not None]
        bounds = f"x={min(p[0] for p in pts)}..{max(p[0] for p in pts)} y={min(p[1] for p in pts)}..{max(p[1] for p in pts)}" if pts else "empty"
        receipt = {"title": self.title or "-", "mode": "+".join(sorted(self._stats["modes"])) or "control",
                   "accepted": self._stats["accepted"], "rejected": self._stats["rejected"],
                   "clipped": self._stats["clipped"], "nonempty": len(pts), "frames": max(1, len(self.frames)),
                   "bounds": bounds}
        self.receipts = (self.receipts + [receipt_line(receipt) + f" · {receipt['bounds']}"])[-HISTORY:]
        return receipt

    # ---- time and sampling -----------------------------------------------
    def active(self) -> bool:
        if self.expires and time.time() > self.expires:
            self.clear()
        return any(v is not None for row in self.current() for v in row)

    def current(self) -> List[List[Cell]]:
        if self.frames:
            idx = int((time.time() - self.started) * self.fps) % len(self.frames)
            return self.frames[idx]
        return self.cells

    def clear(self) -> None:
        self.cells, self.frames, self.title, self.expires = blank(), [], "", 0.0
        self.updated = time.time()

    def sample(self, w: int, h: int) -> List[List[Cell]]:
        """Native 1:1 cells centred when the panel is big enough (text stays legible);
        nearest-neighbour downsampling only when it is smaller."""
        src = self.current()
        w, h = max(0, w), max(0, h)
        if w >= WIDTH and h >= HEIGHT:
            ox, oy = (w - WIDTH) // 2, (h - HEIGHT) // 2
            return [[src[y - oy][x - ox] if 0 <= x - ox < WIDTH and 0 <= y - oy < HEIGHT else None
                     for x in range(w)] for y in range(h)]
        return [[src[min(HEIGHT - 1, int(dy * HEIGHT / max(1, h)))][min(WIDTH - 1, int(dx * WIDTH / max(1, w)))]
                 for dx in range(w)] for dy in range(h)]

    def scan_row(self, h: int) -> int:
        return int(time.time() * 6) % max(1, h)

    def demo(self) -> None:
        self.run("""CLEAR
TITLE CANVAS TEST 40x12
BOX 0 0 40 12 cyan #
TEXT 3 2 white FUTURE CRASH SIGNAL CANVAS
LINE 3 5 35 5 green *
LINE 3 8 35 3 amber /
TEXT 3 9 magenta 40 x 12 LOGICAL CELLS
TTL 30""")

    def dream(self) -> None:
        phase = int(time.time()) % 7
        color = ("cyan", "green", "amber", "magenta")[phase % 4]
        self.run(f"CLEAR\nTITLE SIGNAL DREAM\nCIRCLE 20 6 {2 + phase % 3} {color} o\nPUT 20 6 white *\nLINE 4 10 35 10 dim .\nTTL 60")

    # ---- persistence (so the tool and the face can live in different threads/processes)
    def save(self) -> None:
        store.write_json(roots.canvas, {"cells": self.cells, "frames": self.frames, "fps": self.fps, "title": self.title,
                                        "expires": self.expires, "started": self.started, "updated": self.updated,
                                        "receipts": self.receipts})

    def load(self) -> bool:
        data = store.read_json(roots.canvas, None)
        if not isinstance(data, dict) or float(data.get("updated", 0)) <= self.updated:
            return False
        conv = lambda grid: [[tuple(c) if c else None for c in row] for row in grid]
        self.cells = conv(data.get("cells") or blank())
        self.frames = [conv(f) for f in data.get("frames") or []]
        self.fps, self.title = float(data.get("fps", 4)), str(data.get("title", ""))
        self.expires, self.started = float(data.get("expires", 0)), float(data.get("started", time.time()))
        self.updated, self.receipts = float(data["updated"]), list(data.get("receipts", []))
        return True


def extract_blocks(text: str) -> Tuple[str, List[str]]:
    """Fallback for models without tool calling: pull [[SIGNAL]] blocks out of prose."""
    blocks = BLOCK.findall(text or "")
    return BLOCK.sub("", text or "").strip(), blocks[-2:]


def receipt_line(receipt: Dict[str, object]) -> str:
    return (f"signal · {receipt['title']} · {receipt['mode']} · {receipt['accepted']} ok"
            + (f" · {receipt['rejected']} rejected" if receipt["rejected"] else "")
            + (f" · {receipt['clipped']} clipped" if receipt["clipped"] else "")
            + (f" · {receipt['frames']} frames" if receipt["frames"] > 1 else ""))


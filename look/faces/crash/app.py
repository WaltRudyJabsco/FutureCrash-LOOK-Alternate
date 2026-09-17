"""FUTURE CRASH — the place you inhabit.

    future-crash  (also: fcr, rst)

    A  ask the Oracle (quick, disposable)     X  workstation (a conversation)
    T  threads (scheduled work)               F  fortune       R  observe
    S  clear signal    D  signal demo         M  sound         P  panic
    ?  help            Esc  the shell underneath (exit returns)   Q  quit

The Oracle is a client of the same kernel as LO: same tools, same permission
table, same journal, same lane. It keeps its own temperament and its own
memory. Its authority is `crash.authority` (conservative by default); anything
beyond that is asked for, on screen, every time.
"""
from __future__ import annotations

import os
import queue
import random
import signal as signals
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from ... import __version__, system
from ...daemon import jobs, schedule
from ...kernel import lane
from ...kernel.agent import Agent, Turn, UI
from ...kernel.journal import journal
from ...kernel.ollama import Ollama, current_host
from ...kernel.results import Result
from ...kernel.tools import Tool
from ...mind import personas
from ...settings import settings
from ...term import feedback, keys
from ...term.ansi import RESET, code, fit, strip, style, term_size, width, wrap
from . import text as T
from .canvas import Canvas, extract_blocks, receipt_line

GLYPHS = "0123456789ABCDEF"
SPARKS = "▁▂▃▄▅▆▇█"
COLOR = {"green": code("green"), "green2": code("green2"), "cyan": code("cyan"), "amber": code("amber"),
         "magenta": code("magenta"), "red": code("red"), "white": code("white"), "dim": code("dark"), "gray": code("gray")}
ALT_ON, ALT_OFF, HIDE, SHOW = "\x1b[?1049h", "\x1b[?1049l", "\x1b[?25l", "\x1b[?25h"


def c(name: str, s: str) -> str:
    return COLOR[name] + s + RESET


def spark(values, n: int) -> str:
    vals = list(values)[-max(1, n):]
    return "".join(SPARKS[min(7, int(v * 7.99))] for v in vals).rjust(n, SPARKS[0])


# ---- telemetry ------------------------------------------------------------

@dataclass
class Snapshot:
    cpu: float = 0.0
    mem: float = 0.0
    rx: float = 0.0
    tx: float = 0.0
    disk: float = 0.0
    load: float = 0.0
    uptime: float = 0.0
    cpu_hist: Deque[float] = field(default_factory=lambda: deque([0.0] * 80, maxlen=80))
    mem_hist: Deque[float] = field(default_factory=lambda: deque([0.0] * 80, maxlen=80))


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="telemetry")
        self.snap = Snapshot()
        self.t = system.Telemetry()
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            s = self.snap
            s.cpu = self.t.cpu()
            used, total = system.memory()
            s.mem = used / max(1, total)
            s.rx, s.tx = self.t.net()
            du, dt = system.disk(str(Path.home()))
            s.disk = du / max(1, dt)
            s.load, s.uptime = system.load(), system.uptime()
            s.cpu_hist.append(s.cpu)
            s.mem_hist.append(s.mem)
            self.stop.wait(1.0)


# ---- the Oracle bridge ------------------------------------------------------

class OracleUI(UI):
    """Runs in the Oracle worker thread; talks to the render loop through a queue."""

    def __init__(self, events: "queue.Queue[tuple]"):
        self.events = events

    def token(self, text: str) -> None:
        self.events.put(("token", text))

    def thinking(self, text: str) -> None:
        self.events.put(("thinking", text))

    def tool_start(self, tool: Tool, args: Dict) -> None:
        self.events.put(("tool", f"{tool.label} › {tool.headline(args)}"))

    def receipt(self, tool: Tool, result: Result, elapsed: float) -> None:
        self.events.put(("receipt", (tool.name, result)))

    def notice(self, text: str) -> None:
        self.events.put(("notice", text))

    def ask(self, prompt: str, choices: Dict[str, str], default: str) -> str:
        reply: "queue.Queue[str]" = queue.Queue()
        self.events.put(("approval", (prompt, choices, default, reply)))
        try:
            return reply.get(timeout=300)
        except queue.Empty:
            return default


@dataclass
class Request:
    kind: str       # ambient | fortune | ask | work
    prompt: str


class Oracle(threading.Thread):
    def __init__(self, client: Ollama):
        super().__init__(daemon=True, name="oracle")
        self.client = client
        self.requests: "queue.Queue[Request]" = queue.Queue()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.ui = OracleUI(self.events)
        self.busy: Optional[str] = None
        self.agents: Dict[str, Agent] = {}
        self.current: Optional[Agent] = None

    def agent(self, kind: str) -> Agent:
        model = settings.get("ai.model")
        access = settings.get("crash.authority")
        if kind == "work" and kind in self.agents:
            a = self.agents[kind]
            a.model, a.access, a.ctx.access = model, access, access
            return a
        ambient = kind in ("ambient", "fortune")
        a = Agent(personas.FUTURE_CRASH, client=self.client, model=model, access=access, ui=self.ui,
                  workspace=Path.home(), lane_client="future-crash", remember=kind == "work",
                  lease=not ambient, tool_filter={"signal"} if ambient else None,
                  extra_system="FACE: Future Crash terminal. Replies render in a narrow panel; prefer short paragraphs.")
        if kind == "work":
            self.agents[kind] = a
        return a

    def submit(self, kind: str, prompt: str) -> None:
        self.requests.put(Request(kind, prompt))

    def cancel(self) -> None:
        if self.current:
            self.current.cancel.set()

    def run(self) -> None:
        while True:
            req = self.requests.get()
            self.busy = req.kind
            self.events.put(("start", req.kind))
            try:
                self.current = self.agent(req.kind)
                turn = self.current.turn(req.prompt)
            except Exception as exc:  # boundary: the ambient face must survive any Oracle failure
                turn = Turn(prompt=req.prompt, error=str(exc))
            self.current = None
            self.busy = None
            self.events.put(("done", (req.kind, turn)))


# ---- the face ---------------------------------------------------------------

class FutureCrash:
    def __init__(self, client: Ollama, host_name: str):
        self.client = client
        self.host_name = host_name
        self.rng = random.Random()
        self.sampler = Sampler()
        self.oracle = Oracle(client)
        self.canvas = Canvas()
        self.canvas.load()
        self.mode = "ambient"
        self.running = True
        self.online = False
        self.last_probe = 0.0
        self.fortune = self.rng.choice(T.FORTUNES)
        self.observation = self.rng.choice(T.OBSERVATIONS)
        self.next_ambient = time.time() + self.rng.uniform(20, 40)
        self.rain = [self.rng.randrange(32) for _ in range(400)]
        self.rain_t = time.time()
        self.incident: Optional[str] = None
        self.incident_until = 0.0
        self.event: Optional[Tuple[str, str]] = None
        self.event_until = 0.0
        self.panic: Optional[Tuple[str, str]] = None
        self.panic_until = 0.0
        self.editor = keys.LineEditor()
        self.answer_lines: List[str] = []
        self.answer_scroll = 0
        self.transcript: List[Tuple[str, str]] = []  # (who, text) — who: you | oracle | receipt | notice
        self.streaming = ""
        self.activity = ""
        self.thinking_tail = ""
        self.approval: Optional[tuple] = None
        self.return_mode = "ambient"
        self.thread_index = 0
        self.last_frame: List[str] = []
        self.job_seen = time.time()
        self.pending_kind: Optional[str] = None

    # ---- lifecycle ---------------------------------------------------------
    def probe(self) -> None:
        if time.time() - self.last_probe > 15:
            self.last_probe = time.time()
            threading.Thread(target=lambda: setattr(self, "online", self.client.alive(1.0)), daemon=True).start()

    def run(self) -> int:
        self.sampler.start()
        self.oracle.start()
        self.probe()
        fps = settings.get("crash.fps")
        signals.signal(signals.SIGWINCH, lambda *_: setattr(self, "last_frame", []))
        sys.stdout.write(ALT_ON + HIDE + "\x1b[2J")
        sys.stdout.write("\x1b]0;● FUTURE CRASH\x07")
        try:
            with keys.raw() as fd:
                while self.running:
                    started = time.monotonic()
                    self.tick()
                    self.paint(self.render())
                    budget = max(0.0, 1.0 / fps - (time.monotonic() - started))
                    key = keys.read_key(fd, timeout=budget)
                    while key:
                        self.handle(key)
                        key = keys.read_key(fd, timeout=0)
        finally:
            sys.stdout.write(SHOW + ALT_OFF + "\x1b]0;LOOK\x07")
            sys.stdout.flush()
            self.sampler.stop.set()
        return 0

    def paint(self, rows: List[str]) -> None:
        """Only rewrite rows that changed since the last frame."""
        out = []
        if len(rows) != len(self.last_frame):
            self.last_frame = [""] * len(rows)
            out.append("\x1b[2J")
        for i, row in enumerate(rows):
            if row != self.last_frame[i]:
                out.append(f"\x1b[{i + 1};1H{row}\x1b[K")
        self.last_frame = rows
        if out:
            sys.stdout.write("".join(out))
            sys.stdout.flush()

    # ---- time passes -------------------------------------------------------
    def tick(self) -> None:
        now = time.time()
        self.probe()
        self.canvas.load()
        self.drain_oracle()
        if self.approval and self.mode != "approval":
            self.return_mode, self.mode = self.mode, "approval"
            feedback.play("notify")
        # ambient intelligence only when the lane is genuinely idle
        if (self.mode == "ambient" and settings.get("crash.ambient") and self.online and now > self.next_ambient
                and not self.oracle.busy and self.oracle.requests.empty() and lane.idle(ignore_pid=False)
                and not any(jobs.counts().values())):
            telemetry = f"cpu {self.sampler.snap.cpu:.0%}, memory {self.sampler.snap.mem:.0%}, load {self.sampler.snap.load:.2f}, " \
                        f"uptime {system.duration(self.sampler.snap.uptime)}, time {time.strftime('%H:%M')}"
            self.oracle.submit("ambient", T.AMBIENT_PROMPT.format(telemetry=telemetry))
            self.next_ambient = now + self.rng.uniform(45, 90)
        if not self.incident and self.rng.random() < 0.0009:
            self.incident, self.incident_until = self.rng.choice(T.INCIDENTS), now + self.rng.uniform(0.6, 1.6)
        if self.incident and now > self.incident_until:
            self.incident = None
            self.last_frame = []
        if not self.event and self.rng.random() < 0.0004:
            self.event, self.event_until = self.rng.choice(T.RARE_EVENTS), now + 5
        if self.event and now > self.event_until:
            self.event = None
        # thread results arriving from the background lane
        if now - self.job_seen > 3:
            for ev in journal.events(since=self.job_seen):
                report = (ev.get("data") or {}).get("report") or {}
                if ev.get("name") == "job.done" and report.get("owner") == "future-crash":
                    self.observation = f"THREAD · {report.get('title') or 'task'} · " + " ".join(str(report.get("answer", "")).split())[:160]
                    feedback.play("notify")
            self.job_seen = now

    def drain_oracle(self) -> None:
        while True:
            try:
                kind, payload = self.oracle.events.get_nowait()
            except queue.Empty:
                return
            busy = self.oracle.busy or self.pending_kind
            if kind == "start":
                self.pending_kind = payload
                self.streaming, self.thinking_tail, self.activity = "", "", "listening to the static"
            elif kind == "token":
                self.streaming += payload
                self.activity = ""
            elif kind == "thinking":
                self.thinking_tail = (self.thinking_tail + payload).replace("\n", " ")[-200:]
                self.activity = "thinking"
            elif kind == "tool":
                self.activity = payload
            elif kind == "receipt":
                name, result = payload
                if busy in ("ask", "work"):
                    self.transcript.append(("receipt", f"{result.mark} {result.receipt}"))
                if name == "signal":
                    self.canvas.load()
                    feedback.play("oracle")
                self.activity = ""
            elif kind == "notice":
                self.transcript.append(("notice", payload))
            elif kind == "approval":
                self.approval = payload
            elif kind == "done":
                self.finish(*payload)

    def finish(self, kind: str, turn: Turn) -> None:
        self.pending_kind = None
        self.activity = ""
        answer = turn.answer or ""
        # models without tool calling may still draw in [[SIGNAL]] blocks
        answer, blocks = extract_blocks(answer)
        for block in blocks:
            receipt = self.canvas.run(block)
            self.canvas.save()
            if kind in ("ask", "work"):
                self.transcript.append(("receipt", receipt_line(receipt)))
        if kind == "ambient":
            if answer and not turn.error:
                self.observation = " ".join(answer.split())[:220]
        elif kind == "fortune":
            if answer and not turn.error:
                self.fortune = " ".join(answer.split()).strip('"')[:220]
            feedback.play("fortune")
        else:
            if turn.error:
                answer = f"ORACLE LINK FAILED // {turn.error}"
            self.transcript.append(("oracle", answer or ("SIGNAL UPDATED" if any(n == "signal" for n, _, _ in turn.results) else "…")))
            self.transcript.append(("ledger", turn.ledger()))
            self.transcript = self.transcript[-200:]
            feedback.play("error" if turn.error else "oracle")
            if kind == "ask":
                self.mode = "answer"
                self.answer_scroll = 0
        self.streaming = ""

    # ---- input -------------------------------------------------------------
    def handle(self, key: str) -> None:
        m = self.mode
        if m == "approval":
            prompt, choices, default, reply = self.approval
            if key in ("esc", "ctrl-c"):
                reply.put(default)
            elif key.lower() in choices:
                reply.put(key.lower())
            else:
                return
            self.approval = None
            self.mode = self.return_mode
            return
        if m == "ambient":
            k = key.lower() if len(key) == 1 else key
            if k == "q":
                self.mode = "quit"
            elif k == "esc":
                self.drop_to_shell()
            elif k == "a":
                self.editor = keys.LineEditor(history=self.editor.history)
                self.mode = "ask"
                feedback.play("ask")
            elif k == "x":
                self.editor = keys.LineEditor(history=self.editor.history)
                self.mode = "work"
            elif k == "t":
                self.return_mode, self.mode = "ambient", "threads"
            elif k == "f":
                self.fortune = self.rng.choice(T.FORTUNES)
                feedback.play("fortune")
                if self.online and not self.oracle.busy:
                    self.oracle.submit("fortune", T.FORTUNE_PROMPT.format(seed=self.fortune))
            elif k == "r":
                if self.online and not self.oracle.busy:
                    self.observation = "Oracle is listening to the static…"
                    telemetry = f"cpu {self.sampler.snap.cpu:.0%}, memory {self.sampler.snap.mem:.0%}"
                    self.oracle.submit("ambient", T.AMBIENT_PROMPT.format(telemetry=telemetry))
                    self.next_ambient = time.time() + 60
                else:
                    self.observation = self.rng.choice(T.OBSERVATIONS)
            elif k == "s":
                self.canvas.clear()
                self.canvas.save()
                feedback.play("recover")
            elif k == "d":
                self.canvas.demo()
                self.canvas.save()
                feedback.play("oracle")
            elif k == "m":
                on = not settings.get("feedback.sound")
                settings.set("feedback.sound", on)
                self.observation = f"AUDIO {'ONLINE' if on else 'MUTED'}."
                feedback.play("recover", force=on)
            elif k == "p":
                self.panic, self.panic_until = self.rng.choice(T.PANICS), time.time() + 4.2
                feedback.play("panic")
            elif k in ("?", "h"):
                self.return_mode, self.mode = "ambient", "help"
        elif m in ("ask", "work"):
            if key == "ctrl-c" and self.oracle.busy:
                self.oracle.cancel()
                return
            if key == "ctrl-t" and m == "work":
                self.return_mode, self.mode = "work", "threads"
                return
            if key == "esc" and not self.editor.text:
                self.mode = "ambient"
                return
            outcome = self.editor.feed(key)
            if outcome == "cancel":
                self.editor.text, self.editor.cursor = "", 0
            elif outcome == "submit" and self.editor.text.strip():
                text = self.editor.text.strip()
                self.editor.text, self.editor.cursor = "", 0
                if not self.online:
                    self.transcript.append(("notice", f"Oracle offline at {self.client.base}; input kept."))
                    self.editor.text = text
                    self.editor.cursor = len(text)
                    return
                self.transcript.append(("you", text))
                if m == "ask":
                    self.oracle.agents.pop("ask", None)
                self.oracle.submit(m, text)
                if self.oracle.busy:
                    self.transcript.append(("notice", "queued behind the current Oracle call"))
        elif m == "answer":
            if key in ("up", "k"):
                self.answer_scroll = max(0, self.answer_scroll - 1)
            elif key in ("down", "j"):
                self.answer_scroll += 1
            elif key == "pgdn" or key == " ":
                self.answer_scroll += 10
            elif key == "pgup":
                self.answer_scroll = max(0, self.answer_scroll - 10)
            elif key in ("x", "X"):
                self.mode = "work"
            elif key in ("a", "A"):
                self.mode = "ask"
            else:
                self.mode = "ambient"
        elif m == "threads":
            self.handle_threads(key)
        elif m == "help":
            self.mode = self.return_mode
        elif m == "quit":
            if key.lower() == "y" or key == "enter":
                self.running = False
            else:
                self.mode = "ambient"

    def handle_threads(self, key: str) -> None:
        entries = schedule.load()
        if key in ("esc", "q", "Q"):
            self.mode = self.return_mode
        elif key in ("up", "k"):
            self.thread_index = max(0, self.thread_index - 1)
        elif key in ("down", "j"):
            self.thread_index = min(max(0, len(entries) - 1), self.thread_index + 1)
        elif key == " " and entries:
            e = entries[self.thread_index]
            schedule.update(e["id"], enabled=not e.get("enabled", True))
        elif key in ("d", "D") and entries:
            schedule.remove(entries[self.thread_index]["id"])
            self.thread_index = max(0, self.thread_index - 1)
        elif key in ("n", "N"):
            self.mode = "work"
            self.editor.text = "Every hour, "
            self.editor.cursor = len(self.editor.text)
            self.transcript.append(("notice", "Describe the thread; the Oracle will schedule it with read-only authority."))

    # ---- shell underneath ----------------------------------------------------
    def drop_to_shell(self) -> None:
        shell = os.environ.get("SHELL") or "/bin/sh"
        sys.stdout.write(SHOW + ALT_OFF + "\x1b]0;◌ FUTURE CRASH · SHELL\x07")
        print(style("FUTURE CRASH is still running above this shell. Type exit to return.", "green2"))
        sys.stdout.flush()
        env = dict(os.environ, FUTURE_CRASH_ACTIVE="1")
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            # give the child a normal terminal, then take raw mode back
            cooked = termios.tcgetattr(fd)
            cooked[3] |= termios.ECHO | termios.ICANON | termios.ISIG
            termios.tcsetattr(fd, termios.TCSADRAIN, cooked)
            subprocess.call([shell, "-i"], env=env)
        except OSError as exc:
            print(f"shell failed: {exc}")
            time.sleep(1.5)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(ALT_ON + HIDE + "\x1b]0;● FUTURE CRASH\x07\x1b[2J")
            sys.stdout.flush()
            self.last_frame = []

    # ---- drawing -----------------------------------------------------------
    def render(self) -> List[str]:
        w, h = term_size()
        w, h = max(72, w - 1), max(22, h)
        body = {"ambient": self.render_ambient, "ask": self.render_work, "work": self.render_work,
                "answer": self.render_answer, "threads": self.render_threads, "help": self.render_help,
                "quit": self.render_quit, "approval": self.render_approval}[self.mode](w, h)
        rows = (body + [""] * h)[:h]
        return [fit(r, w) for r in rows]

    def header(self, w: int) -> str:
        left = c("cyan", "\x1b[1m" + time.strftime("%H:%M:%S")) + c("dim", "  " + time.strftime("%Y-%m-%d"))
        mid = "\x1b[1m" + c("green", "  FUTURE CRASH") + c("green2", f" // ZERO {__version__}")
        link = c("green", "●") if self.online else c("red", "●")
        right = c("dim", f"{settings.get('ai.model')} @ {self.host_name} ") + link
        gap = max(1, w - width(left) - width(mid) - width(right))
        return left + mid + " " * gap + right

    def box(self, title: str, lines: List[str], w: int, h: int, color: str) -> List[str]:
        inner = w - 4
        top = c(color, "╭─ ") + c("white", title) + c(color, " " + "─" * max(0, w - width(title) - 5) + "╮")
        out = [top]
        for i in range(h - 2):
            line = lines[i] if i < len(lines) else ""
            out.append(c(color, "│ ") + fit(line, inner) + c(color, " │"))
        out.append(c(color, "╰" + "─" * (w - 2) + "╯"))
        return out

    def rain_rows(self, rw: int, rh: int) -> List[str]:
        if time.time() - self.rain_t > 0.12:
            self.rain = [(r + 1) % 32 for r in self.rain]
            self.rain_t = time.time()
        overlay = self.canvas.sample(rw, rh) if self.canvas.active() else None
        scan = self.canvas.scan_row(rh) if overlay else -1
        rows = []
        for y in range(rh):
            chars = []
            for x in range(rw):
                painted = overlay[y][x] if overlay else None
                if painted:
                    ch, col = painted
                    chars.append(("\x1b[1m" if y == scan else "") + COLOR.get(col if col != "dim" else "dim", COLOR["cyan"]) + ch + RESET)
                    continue
                delta = (self.rain[x % len(self.rain)] - y) % 32
                if delta == 0:
                    chars.append(c("cyan", self.rng.choice(GLYPHS)))
                elif delta < 4:
                    chars.append(c("green", self.rng.choice(GLYPHS)))
                elif delta < 8 and self.rng.random() < 0.25:
                    chars.append(c("green2", self.rng.choice("01")))
                else:
                    chars.append(" ")
            rows.append("".join(chars))
        return rows

    def render_ambient(self, w: int, h: int) -> List[str]:
        s = self.sampler.snap
        menu = "[esc] shell  [a] ask  [x] workstation  [t] threads  [f] fortune  [r] observe  [s] signal  [d] demo  [m] mute  [?] help  [p] panic  [q] quit"
        menu_rows = wrap(menu, w)
        fortune_rows = (wrap(self.fortune, w - 4)[:3] + ["", "", ""])[:3]
        panel_h = max(10, h - 3 - (1 + 3 + len(menu_rows)))
        left_w = max(34, int(w * 0.42))
        right_w = w - left_w - 3
        active = sum(1 for e in schedule.load() if e.get("enabled", True))
        q = jobs.counts()
        left = [
            c("dim", "MACHINE"),
            f"CPU   {c('green', f'{s.cpu * 100:5.1f}%')}  {c('green2', spark(s.cpu_hist, left_w - 20))}",
            f"MEM   {c('cyan', f'{s.mem * 100:5.1f}%')}  {c('cyan', spark(s.mem_hist, left_w - 20))}",
            f"NET   {c('amber', '↓' + system.human(s.rx) + '/s ↑' + system.human(s.tx) + '/s')}",
            f"DISK  {s.disk * 100:5.1f}%   LOAD {s.load:.2f}",
            f"UP    {system.duration(s.uptime)}",
            "",
            c("dim", "ORACLE"),
            f"LINK       {c('green', 'READY') if self.online else c('red', 'OFFLINE')}",
            f"MODEL      {c('white', settings.get('ai.model'))}",
            f"AUTHORITY  {c('green', settings.get('crash.authority').upper())}",
            f"LANE       {c('cyan', ', '.join(lane.busy_clients()) or 'idle')}",
            f"QUEUE      {c('cyan', str(q.get('job', 0)) + ' jobs · ' + str(q.get('reflect', 0)) + ' reflections')}",
            f"THREADS    {c('cyan', f'{active} active')}",
            f"AUDIO      {c('cyan', 'ON' if settings.get('feedback.sound') else 'MUTED')}",
        ]
        if self.oracle.busy:
            left.append(f"NOW        {c('amber', (self.activity or self.oracle.busy)[:left_w - 16])}")
        title = "SIGNAL FIELD" + (f" // {self.canvas.title}" if self.canvas.active() and self.canvas.title else "")
        rain_h = max(4, panel_h - 7)
        right = [c("dim", title)] + self.rain_rows(max(10, right_w - 4), rain_h) + ["", c("amber", "ORACLE OBSERVATION")]
        right += wrap(self.observation, right_w - 4)[:2]
        lb = self.box("TELEMETRY", left, left_w, panel_h, "green2")
        rb = self.box("SIGNAL", right, right_w, panel_h, "cyan")
        rows = [self.header(w), ""] + [lb[i] + "   " + rb[i] for i in range(panel_h)]
        rows += ["", c("green2", "FORTUNE //")] + [c("green2", "  " + f) for f in fortune_rows]
        rows += [c("dim", r) for r in menu_rows]
        return self.glitch(rows, w)

    def glitch(self, rows: List[str], w: int) -> List[str]:
        now = time.time()
        if self.incident:
            phase = int(now * 15) % 8
            mid = max(3, len(rows) // 2)
            if self.incident == "signal_loss":
                rows[mid] = c("white", "\x1b[1m" + " NO SIGNAL // REACQUIRING LOCAL REALITY ".center(w, "░"))
            elif self.incident == "memory_leak_theater":
                for n in range(5):
                    if 3 + n < len(rows):
                        rows[3 + n] = c("red", f"MEMORY {117 + n * 83:03d}%  THIS IS FINE")
            elif self.incident == "cursor_echo":
                rows[mid] = c("magenta", ("_" * (8 + phase * 3)).center(w))
            elif self.incident == "static_infiltration":
                for r in range(3, min(len(rows) - 2, 8)):
                    rows[r] = c("dim", "".join(self.rng.choice(" .:░▒▓#") for _ in range(w // 3)))
            else:
                for r in range(3, min(len(rows) - 2, 3 + phase)):
                    plain = strip(rows[r])
                    if plain:
                        shift = (phase + r) % max(1, min(16, len(plain)))
                        rows[r] = c("magenta" if r % 2 else "red", plain[shift:] + plain[:shift])
        if self.event:
            mid = max(4, len(rows) // 2 - 1)
            rows[mid] = c("amber", "\x1b[1m" + (" " + self.event[0] + " ").center(w, "─"))
            rows[mid + 1] = c("white", self.event[1].center(w))
        if self.panic and now < self.panic_until:
            phase = int((4.2 - (self.panic_until - now)) * 4)
            mid = max(3, len(rows) // 2 - 2)
            lines = [c("red", "\x1b[1m" + f" !!! {self.panic[0]} !!! "), c("magenta", self.panic[1]),
                     c("amber", T.PANIC_LINES[phase % len(T.PANIC_LINES)]),
                     c("red", "RECOVERY VECTOR " + "▓" * (phase + 2) + "░" * max(0, 13 - phase))]
            for i, line in enumerate(lines):
                rows[mid + i] = " " * max(0, (w - width(line)) // 2) + line
        elif self.panic:
            self.panic = None
            feedback.play("recover")
        return rows

    def conversation_rows(self, cw: int, ch: int) -> List[str]:
        rows: List[str] = []
        for who, text in self.transcript:
            if who == "you":
                rows += [c("green", "you › ") + wrap(text, cw - 6)[0]] + ["      " + r for r in wrap(text, cw - 6)[1:]]
            elif who == "oracle":
                from ...term.markdown import render
                rendered = render(text).rstrip("\n").splitlines()
                rows += [c("cyan", "oracle ›")] + ["  " + r for line in rendered for r in (wrap(line, cw - 2) or [""])]
            elif who == "receipt":
                rows += [c("dim", "  " + r) for r in wrap(text, cw - 2)]
            elif who == "ledger":
                rows += [c("dim", "  " + text), ""]
            else:
                rows += [c("amber", "  · " + r) for r in wrap(text, cw - 4)]
        if self.oracle.busy in ("ask", "work") or self.pending_kind in ("ask", "work"):
            if self.streaming:
                rows += [c("cyan", "oracle ›")] + ["  " + r for r in wrap(self.streaming, cw - 2)]
            spin = feedback.SPINNER[int(time.time() * 10) % len(feedback.SPINNER)]
            detail = self.activity or ("thinking · " + self.thinking_tail[-(cw - 16):] if self.thinking_tail else "listening")
            rows.append(c("amber", f"  {spin} {detail}"))
        return rows[-ch:]

    def render_work(self, w: int, h: int) -> List[str]:
        title = "ASK THE ORACLE" if self.mode == "ask" else "WORKSTATION"
        hint = "quick · disposable" if self.mode == "ask" else "a conversation · ctrl-t threads"
        signal_w = 44 if w >= 110 else 0
        cw = w - signal_w - (3 if signal_w else 0)
        ch = h - 6
        conv = self.box(f"{title} // {hint}", self.conversation_rows(cw - 4, ch - 2), cw, ch, "cyan")
        rows = [self.header(w), ""]
        if signal_w:
            sig = self.box("SIGNAL" + (f" // {self.canvas.title}" if self.canvas.title else ""),
                           self.rain_rows(signal_w - 4, min(ch - 2, 14)) + [""] + [c("dim", r) for r in self.canvas.receipts[-3:]],
                           signal_w, ch, "green2")
            rows += [conv[i] + "   " + sig[i] for i in range(ch)]
        else:
            rows += conv
        cursor_text = self.editor.text[:self.editor.cursor] + "\x1b[7m" + (self.editor.text[self.editor.cursor:self.editor.cursor + 1] or " ") + RESET + self.editor.text[self.editor.cursor + 1:]
        rows += ["", c("green", "› ") + cursor_text, c("dim", "enter send · esc back · ctrl-c cancel oracle · ↑ history")]
        return rows

    def render_answer(self, w: int, h: int) -> List[str]:
        body: List[str] = []
        from ...term.markdown import render
        last = next((t for who, t in reversed(self.transcript) if who == "oracle"), "")
        receipts = []
        for who, t in reversed(self.transcript):
            if who == "you":
                break
            if who in ("receipt", "ledger"):
                receipts.insert(0, t)
        for line in render(last).rstrip().splitlines():
            body += wrap(line, w - 8) or [""]
        body += [""] + [c("dim", r) for r in receipts]
        view = body[self.answer_scroll:self.answer_scroll + h - 6]
        rows = [self.header(w), ""] + self.box("THE ORACLE SAYS", view, w, h - 4, "cyan")
        rows.append(c("dim", "↑↓ scroll · x continue in workstation · a ask again · any other key returns"))
        return rows

    def render_threads(self, w: int, h: int) -> List[str]:
        entries = schedule.load()
        lines = [c("dim", "Scheduled work shared with LO. Background runs are read-only and never ask questions."), ""]
        if not entries:
            lines.append(c("amber", "No threads. Press n to describe one."))
        for i, e in enumerate(entries):
            nxt = time.strftime("%a %H:%M", time.localtime(float(e.get("next") or 0))) if e.get("enabled", True) else "paused"
            row = f"{'●' if e.get('enabled', True) else '○'} {e['id']}  {schedule.describe(e):<11} {nxt:<10} {e.get('owner', 'lo'):<13} {e.get('title', '')}"
            lines.append(("\x1b[7m" + row + RESET) if i == self.thread_index else row)
        recent = [ev for ev in journal.events(limit=400) if ev.get("name") == "job.done"][-6:]
        if recent:
            lines += ["", c("dim", "RECENT RESULTS")]
            for ev in reversed(recent):
                rep = (ev.get("data") or {}).get("report") or {}
                lines.append(c("cyan", time.strftime("%H:%M ", time.localtime(ev.get("t", 0)))) + " ".join(str(rep.get("answer", "")).split())[:w - 12])
        rows = [self.header(w), ""] + self.box("THREADS", lines, w, h - 4, "amber")
        rows.append(c("dim", "↑↓ select · space pause/resume · d delete · n new · esc back"))
        return rows

    def render_help(self, w: int, h: int) -> List[str]:
        text = (__doc__ or "").strip().splitlines()
        return [self.header(w), ""] + self.box("HELP", text, w, min(h - 3, len(text) + 4), "green2") + [c("dim", "any key returns")]

    def render_quit(self, w: int, h: int) -> List[str]:
        rows = self.render_ambient(w, h)
        mid = h // 2 - 1
        box = self.box("QUIT", ["", "  Leave Future Crash?   [y] yes   [any key] stay", ""], 52, 5, "red")
        for i, line in enumerate(box):
            rows[mid + i] = " " * ((w - 52) // 2) + line
        return rows

    def render_approval(self, w: int, h: int) -> List[str]:
        rows = self.render_work(w, h) if self.return_mode in ("ask", "work") else self.render_ambient(w, h)
        if not self.approval:
            return rows
        prompt, choices, default, _ = self.approval
        lines = [""] + ["  " + l for l in prompt.splitlines()] + [""] + [f"  [{k}] {v}" for k, v in choices.items()] + ["", "  esc = no"]
        bw = min(w - 8, max(60, max(width(l) for l in lines) + 6))
        box = self.box("THE ORACLE ASKS PERMISSION", lines, bw, len(lines) + 2, "amber")
        top = max(2, (h - len(box)) // 2)
        for i, line in enumerate(box):
            if top + i < len(rows):
                rows[top + i] = " " * ((w - bw) // 2) + line
        return rows


def main(argv: List[str]) -> int:
    if os.environ.get("FUTURE_CRASH_ACTIVE") == "1" and "--nested" not in argv:
        print("Already inside Future Crash. Type exit to return to it.")
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("future-crash needs an interactive terminal.")
        return 1
    personas.ensure_profile()
    name, url = current_host()
    for a in argv:
        if a.startswith("@"):
            from ...kernel.ollama import resolve_host
            name, url = resolve_host(a[1:])
            if not url:
                print(f"no saved host {a[1:]}")
                return 1
    os.environ["FUTURE_CRASH_ACTIVE"] = "1"
    return FutureCrash(Ollama(url), name).run()

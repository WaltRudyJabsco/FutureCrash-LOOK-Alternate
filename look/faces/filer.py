"""The LOOK filer: see a directory, narrow it, act on it.

    lk [PATH|WORD]          interactive in the current (or given) directory
    lk detail|dirs|files|tree|recent|size [PATH]

Typing filters (lower-case letters, digits, . - _ and space). Upper-case letters are commands:

    ↑↓ PgUp PgDn        move            →/Enter    open dir · view file
    ←                   parent          Tab        mark · A all · X clear
    C  copy to          M  move to      R  remove (to trash)      N  new file or dir/
    U  undo last        Y  copy path    O  open in app            E  edit in $EDITOR
    L  hand selection to LO             G  go: cd the shell here and quit
    P  preview pane     V  cycle view   H  hidden files           F  find under ~
    ?  help             Q  quit         Esc  clear filter → clear marks → quit

Every change goes through the journal: `U` here or `lk undo` anywhere reverses it.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Set

from ..kernel import fsops
from ..kernel.journal import journal
from ..kernel.results import Result
from ..system import human
from ..term import keys
from ..term.ansi import code, fit, strip, style, term_size, width, RESET

MODES = ("detail", "dirs", "files", "tree", "recent", "size")
ALT_ON, ALT_OFF, HIDE, SHOW, HOME_CUR, CLEAR_EOL = "\x1b[?1049h", "\x1b[?1049l", "\x1b[?25l", "\x1b[?25h", "\x1b[H", "\x1b[K"
FILTER_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-_ ~/")
KIND_STYLE = {"dir": "blue", "exec": "green", "link": "magenta", "image": "amber", "media": "magenta", "doc": "cyan", "file": "white"}


@dataclass
class Entry:
    path: Path
    name: str
    is_dir: bool
    size: int
    mtime: float
    kind: str
    depth: int = 0


def _kind(p: Path, st: os.stat_result, is_dir: bool) -> str:
    if p.is_symlink():
        return "link"
    if is_dir:
        return "dir"
    ext = p.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".tiff"}:
        return "image"
    if ext in {".mp4", ".mov", ".mkv", ".mp3", ".flac", ".wav", ".m4a", ".webm"}:
        return "media"
    if ext in {".pdf", ".md", ".txt", ".docx", ".epub", ".rtf"}:
        return "doc"
    if st.st_mode & stat.S_IXUSR:
        return "exec"
    return "file"


def _entry(p: Path, depth: int = 0) -> Optional[Entry]:
    try:
        st = p.lstat()
        is_dir = stat.S_ISDIR(p.stat().st_mode) if p.is_symlink() and p.exists() else stat.S_ISDIR(st.st_mode)
    except OSError:
        return None
    return Entry(p, p.name, is_dir, st.st_size, st.st_mtime, _kind(p, st, is_dir), depth)


def read_entries(cwd: Path, mode: str, hidden: bool = False, deadline: float = 2.5) -> List[Entry]:
    started = time.monotonic()
    visible = lambda name: hidden or not name.startswith(".")
    if mode in ("detail", "dirs", "files", "size"):
        try:
            children = [e for e in (_entry(p) for p in cwd.iterdir() if visible(p.name)) if e]
        except OSError:
            return []
        if mode == "dirs":
            children = [e for e in children if e.is_dir]
        elif mode == "files":
            children = [e for e in children if not e.is_dir]
        if mode == "size":
            for e in children:
                if e.is_dir and time.monotonic() - started < deadline:
                    e.size = _dir_size(e.path, started + deadline)
            return sorted(children, key=lambda e: -e.size)
        return sorted(children, key=lambda e: (not e.is_dir, e.name.casefold()))
    if mode == "tree":
        out: List[Entry] = []

        def walk(d: Path, depth: int):
            if depth > 3 or len(out) > 3000 or time.monotonic() - started > deadline:
                return
            try:
                kids = sorted((p for p in d.iterdir() if visible(p.name)), key=lambda p: (not p.is_dir(), p.name.casefold()))
            except OSError:
                return
            for p in kids:
                e = _entry(p, depth)
                if not e:
                    continue
                out.append(e)
                if e.is_dir and e.kind != "link" and p.name not in {".git", "node_modules", "__pycache__"}:
                    walk(p, depth + 1)
        walk(cwd, 0)
        return out
    # recent: newest files anywhere below, bounded
    found: List[Entry] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if visible(d) and d not in {"node_modules", ".git", "__pycache__", "Library"}]
        for name in filenames:
            if visible(name):
                e = _entry(Path(dirpath) / name, len(Path(dirpath).relative_to(cwd).parts))
                if e:
                    found.append(e)
        if len(found) > 20000 or time.monotonic() - started > deadline:
            break
    found.sort(key=lambda e: -e.mtime)
    return found[:400]


def _dir_size(path: Path, until: float) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
        if time.monotonic() > until:
            break
    return total


def matches(entry: Entry, query: str, cwd: Path) -> bool:
    words = query.casefold().split()
    if not words:
        return True
    hay = (str(entry.path.relative_to(cwd)) if entry.depth else entry.name).casefold()
    return all(w in hay for w in words)


def age(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 86400 * 60:
        return f"{int(d // 86400)}d"
    return time.strftime("%Y-%m", time.localtime(ts))


def display_path(p: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(p.relative_to(home)) if p != home else "~"
    except ValueError:
        return str(p)


def copy_to_clipboard(text: str) -> bool:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), timeout=2, check=False)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    import base64  # OSC 52: works over SSH in most modern terminals
    sys.stdout.write(f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07")
    return True


def preview_lines(entry: Entry, cols: int, rows: int) -> List[str]:
    p = entry.path
    head = [style(entry.name, "bold"), style(f"{'folder' if entry.is_dir else human(entry.size)} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry.mtime))}", "muted"), ""]
    if entry.is_dir:
        try:
            kids = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.casefold()))
        except OSError as exc:
            return head + [style(str(exc), "bad")]
        body = [style(c.name + "/", "blue") if c.is_dir() else c.name for c in kids if not c.name.startswith(".")]
        return head + (body or [style("(empty)", "muted")])
    if entry.kind == "image" and shutil.which("chafa"):
        try:
            out = subprocess.run(["chafa", "--size", f"{cols}x{max(4, rows - 4)}", "--format", "symbols", str(p)],
                                 capture_output=True, text=True, timeout=3).stdout
            return head + out.splitlines()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        with open(p, "rb") as fh:
            raw = fh.read(64_000)
    except OSError as exc:
        return head + [style(str(exc), "bad")]
    if b"\x00" in raw[:4096]:
        return head + [style("binary file", "muted")]
    text = raw.decode("utf-8", errors="replace").expandtabs(4)
    return head + [style(line, "gray") if line.strip().startswith(("#", "//")) else line for line in text.splitlines()[:rows]]


class Filer:
    def __init__(self, cwd: Path, mode: str = "detail", query: str = "", on_go: Optional[Callable[[Path], None]] = None):
        self.cwd = cwd.resolve()
        self.mode = mode
        self.query = query
        self.hidden = False
        self.preview = True
        self.index = 0
        self.top = 0
        self.marked: Set[Path] = set()
        self.notice = ""
        self.entries: List[Entry] = []
        self.visible: List[Entry] = []
        self.running = True
        self.on_go = on_go
        self.help = False
        self.reload()

    # ---- data --------------------------------------------------------------
    def reload(self, keep: Optional[Path] = None) -> None:
        self.entries = read_entries(self.cwd, self.mode, self.hidden)
        self.refilter(keep)

    def refilter(self, keep: Optional[Path] = None) -> None:
        self.visible = [e for e in self.entries if matches(e, self.query, self.cwd)]
        if keep:
            self.index = next((i for i, e in enumerate(self.visible) if e.path == keep), 0)
        self.index = max(0, min(self.index, len(self.visible) - 1))

    @property
    def current(self) -> Optional[Entry]:
        return self.visible[self.index] if self.visible else None

    def targets(self) -> List[Path]:
        if self.marked:
            return sorted(self.marked, key=lambda p: str(p).casefold())
        return [self.current.path] if self.current else []

    # ---- rendering -----------------------------------------------------------
    def frame(self) -> str:
        cols, rows = term_size()
        cols -= 1
        list_rows = rows - 4
        show_preview = self.preview and cols >= 100 and self.current is not None
        list_w = cols if not show_preview else int(cols * 0.55)
        prev_w = cols - list_w - 3
        if self.index < self.top:
            self.top = self.index
        if self.index >= self.top + list_rows:
            self.top = self.index - list_rows + 1

        title = style(" LOOK ", "reverse", "accent") + " " + style(display_path(self.cwd), "path")
        info = [self.mode, f"{len(self.visible)}/{len(self.entries)}"]
        if self.marked:
            info.append(style(f"{len(self.marked)} marked", "warn"))
        if self.hidden:
            info.append("hidden")
        head = title + style("  " + " · ".join(info), "muted")
        filt = style("  filter › ", "accent") + self.query + style("▏", "accent") if self.query else style("  type to filter · ? help", "dark")
        lines = [fit(head, cols), fit(filt, cols)]

        body: List[str] = []
        for i in range(self.top, min(len(self.visible), self.top + list_rows)):
            body.append(self._row(self.visible[i], i == self.index, list_w))
        if not self.visible:
            body.append(style("  nothing matches" if self.query else "  (empty)", "muted"))
        body += [""] * (list_rows - len(body))

        if show_preview:
            pv = preview_lines(self.current, prev_w, list_rows)
            pv += [""] * (list_rows - len(pv))
            for i in range(list_rows):
                lines.append(fit(body[i], list_w) + style(" │ ", "dark") + fit(pv[i], prev_w))
        else:
            lines += [fit(b, cols) for b in body]
        hint = "Tab mark · C copy · M move · R remove · N new · U undo · L lo · G go · O open · P preview · V view · Q quit"
        lines.append(fit(style(self.notice, "warn") if self.notice else style(hint, "dark"), cols))
        if self.help:
            lines = self._help_overlay(lines, cols)
        return HOME_CUR + "\n".join(l + CLEAR_EOL for l in lines[:rows]) + "\x1b[J"

    def _row(self, e: Entry, selected: bool, w: int) -> str:
        mark = style("●", "warn") if e.path in self.marked else " "
        indent = "  " * e.depth if self.mode == "tree" else ""
        name = (str(e.path.relative_to(self.cwd)) if self.mode == "recent" else e.name) + ("/" if e.is_dir else "")
        name = style(indent + name, KIND_STYLE.get(e.kind, "white"))
        size = "" if e.is_dir and self.mode != "size" else human(e.size)
        meta = f"{size:>7}  {age(e.mtime):>7}"
        room = max(10, w - 20)
        row = f"{mark} {fit(name, room)}{style(meta, 'muted')}"
        if selected:
            return code("reverse") + strip(row) + RESET if not os.environ.get("NO_COLOR") else "> " + strip(row)
        return row

    def _help_overlay(self, lines: List[str], cols: int) -> List[str]:
        text = (__doc__ or "").split("\n\n", 2)[2].rstrip().splitlines()
        box_w = min(cols - 4, max(width(t) for t in text) + 4)
        start = 3
        for i, t in enumerate([""] + text + ["", "   any key to close"]):
            if start + i < len(lines) - 1:
                lines[start + i] = "  " + style(fit("  " + t, box_w), "reverse")
        return lines

    # ---- actions -------------------------------------------------------------
    def prompt(self, label: str, initial: str = "") -> Optional[str]:
        ed = keys.LineEditor(initial)
        while True:
            cols, rows = term_size()
            sys.stdout.write(f"\x1b[{rows};1H" + fit(style(f" {label} › ", "reverse", "warn") + " " + ed.text + style("▏", "warn"), cols - 1) + CLEAR_EOL)
            sys.stdout.flush()
            key = keys.read_key()
            if key == "tab":
                ed.text = self._complete(ed.text)
                ed.cursor = len(ed.text)
                continue
            outcome = ed.feed(key or "")
            if outcome == "submit":
                return ed.text.strip()
            if outcome == "cancel":
                return None

    def _complete(self, text: str) -> str:
        raw = os.path.expanduser(text or "")
        base = Path(raw) if os.path.isabs(raw) else self.cwd / raw
        parent, stem = (base, "") if text.endswith("/") else (base.parent, base.name)
        try:
            options = sorted(p.name for p in parent.iterdir() if p.is_dir() and p.name.startswith(stem))
        except OSError:
            return text
        if len(options) == 1:
            return text[: len(text) - len(stem)] + options[0] + "/"
        if options:
            common = os.path.commonprefix(options)
            return text[: len(text) - len(stem)] + common
        return text

    def resolve(self, raw: str) -> Path:
        p = Path(os.path.expanduser(raw))
        return (p if p.is_absolute() else self.cwd / p)

    def act(self, result: Result, keep: Optional[Path] = None) -> None:
        if result.undo:
            journal.record_tool("filer", _FakeTool(result), {}, result)
        self.notice = f"{result.mark} {result.receipt}" + (" · U undoes" if result.undo else "")
        self.marked = {p for p in self.marked if p.exists()}
        self.reload(keep)

    def handle(self, key: str) -> None:
        fsops.BASE.set(self.cwd)  # receipts read relative to where you are looking
        self.notice = ""
        if self.help:
            self.help = False
            return
        e = self.current
        n = len(self.visible)
        rows = term_size()[1] - 4
        if key in ("up", "K", "ctrl-p"):
            self.index = max(0, self.index - 1)
        elif key in ("down", "J", "ctrl-n"):
            self.index = min(n - 1, self.index + 1)
        elif key == "pgup":
            self.index = max(0, self.index - rows)
        elif key == "pgdn":
            self.index = min(n - 1, self.index + rows)
        elif key == "home":
            self.index = 0
        elif key == "end":
            self.index = n - 1
        elif key in ("right", "enter") and e:
            if e.is_dir:
                self.cwd, self.query, self.index, self.top = e.path.resolve(), "", 0, 0
                self.reload()
            else:
                self.view(e.path)
        elif key == "left" or (key == "backspace" and not self.query):
            old = self.cwd
            self.cwd, self.query, self.top = self.cwd.parent, "", 0
            self.reload(keep=old)
        elif key == "backspace":
            self.query = self.query[:-1]
            self.refilter()
        elif key == "ctrl-u":
            self.query = ""
            self.refilter()
        elif key == "esc":
            if self.query:
                self.query = ""
                self.refilter()
            elif self.marked:
                self.marked.clear()
            else:
                self.running = False
        elif key in ("Q", "ctrl-c", "ctrl-d"):
            self.running = False
        elif key == "tab" and e:
            self.marked.symmetric_difference_update({e.path})
            self.index = min(n - 1, self.index + 1)
        elif key == "A":
            self.marked |= {v.path for v in self.visible}
        elif key == "X":
            self.marked.clear()
        elif key == "?":
            self.help = True
        elif key == "P":
            self.preview = not self.preview
        elif key == "H":
            self.hidden = not self.hidden
            self.reload()
        elif key == "V":
            self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
            self.notice = f"view: {self.mode}"
            self.reload()
        elif key in ("C", "M") and self.targets():
            verb = "copy" if key == "C" else "move"
            dest = self.prompt(f"{verb} {len(self.targets())} to", display_path(self.cwd) + "/")
            if dest:
                fn = fsops.copy_paths if key == "C" else fsops.move_paths
                self.act(fn(self.targets(), self.resolve(dest), dest))
                if key == "M":
                    self.marked.clear()
        elif key == "R" and self.targets():
            targets = self.targets()
            answer = self.prompt(f"remove {len(targets)} ({targets[0].name}{'…' if len(targets) > 1 else ''})? y/N")
            if answer and answer.lower().startswith("y"):
                self.act(fsops.remove_paths(targets))
                self.marked.clear()
        elif key == "N":
            name = self.prompt("new (end with / for a folder)")
            if name:
                target = self.resolve(name)
                self.act(fsops.make_directory(target) if name.endswith("/") else fsops.touch(target, parents=True), keep=target)
        elif key == "U":
            r = journal.undo()
            self.notice = f"{r.mark} {r.receipt}"
            self.reload()
        elif key == "Y" and self.targets():
            copy_to_clipboard("\n".join(str(p) for p in self.targets()))
            self.notice = f"copied {len(self.targets())} path(s)"
        elif key == "O" and e:
            from ..tools.host import launch
            r = launch(e.path)
            self.notice = f"{r.mark} {r.receipt}"
        elif key == "E" and e and not e.is_dir:
            self.suspend(lambda: subprocess.call([os.environ.get("EDITOR") or ("nvim" if shutil.which("nvim") else "vi"), str(e.path)]))
            self.reload(keep=e.path)
        elif key == "L" and self.targets():
            targets = self.targets()
            common = Path(os.path.commonpath([str(p if p.is_dir() else p.parent) for p in targets]))

            def run_lo():
                from . import lo
                prev = os.getcwd()
                os.chdir(common)
                try:
                    lo.main([], selected=[str(p) for p in targets])
                finally:
                    os.chdir(prev)
            self.suspend(run_lo)
            self.notice = f"LO returned · {len(targets)} path(s) were in context"
            self.reload()
        elif key == "G":
            target = e.path if e and e.is_dir else self.cwd
            if self.on_go:
                self.on_go(target)
            self.running = False
        elif key == "F":
            word = self.prompt("find under ~")
            if word:
                self.find(word)
        elif key.startswith("paste:"):
            self.query += key[6:].strip()
            self.refilter()
        elif len(key) == 1 and key in FILTER_CHARS:
            self.query += key
            self.index = 0
            self.refilter()

    def find(self, word: str) -> None:
        home = Path.home()
        hits: List[Entry] = []
        started = time.monotonic()
        low = word.casefold()
        for dirpath, dirnames, filenames in os.walk(home):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"Library", "node_modules", "__pycache__"}]
            for name in dirnames + filenames:
                if low in name.casefold():
                    e = _entry(Path(dirpath) / name)
                    if e:
                        hits.append(e)
            if len(hits) > 500 or time.monotonic() - started > 4:
                break
        self.cwd, self.mode, self.query = home, "recent", ""
        for h in hits:
            h.depth = len(h.path.relative_to(home).parts) - 1
        self.entries = sorted(hits, key=lambda e: (-e.is_dir, len(e.path.parts)))
        self.refilter()
        self.notice = f"{len(hits)} names containing {word!r} under ~ · → opens"

    def view(self, path: Path) -> None:
        lines = preview_lines(_entry(path), term_size()[0] - 4, 100000)
        top = 0
        while True:
            cols, rows = term_size()
            page = lines[top: top + rows - 1]
            out = [fit("  " + l, cols - 1) for l in page] + [""] * (rows - 1 - len(page))
            out.append(fit(style(f" {path.name} · {top + 1}/{len(lines)} · ↑↓ PgUp PgDn · q back", "reverse", "muted"), cols - 1))
            sys.stdout.write(HOME_CUR + "\n".join(l + CLEAR_EOL for l in out))
            sys.stdout.flush()
            k = keys.read_key()
            if k in ("q", "Q", "esc", "left", "ctrl-c"):
                return
            if k in ("down", "j", "enter"):
                top = min(max(0, len(lines) - rows + 1), top + 1)
            elif k in ("up", "k"):
                top = max(0, top - 1)
            elif k in ("pgdn", " "):
                top = min(max(0, len(lines) - rows + 1), top + rows - 1)
            elif k == "pgup":
                top = max(0, top - rows + 1)

    def suspend(self, fn: Callable[[], object]) -> None:
        sys.stdout.write(SHOW + ALT_OFF)
        sys.stdout.flush()
        try:
            fn()
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write(ALT_ON + HIDE + "\x1b[2J")
            sys.stdout.flush()

    def run(self) -> None:
        sys.stdout.write(ALT_ON + HIDE + "\x1b[2J")
        try:
            with keys.raw() as fd:
                while self.running:
                    sys.stdout.write(self.frame())
                    sys.stdout.flush()
                    key = keys.read_key(fd, timeout=1.0)
                    if key:
                        self.handle(key)
        finally:
            sys.stdout.write(SHOW + ALT_OFF)
            sys.stdout.flush()


class _FakeTool:
    """Journal records for filer actions carry the filer as the tool name."""
    def __init__(self, result: Result):
        self.name = "filer"
        self.effect = "change"


def print_listing(cwd: Path, mode: str, query: str = "") -> None:
    entries = [e for e in read_entries(cwd, mode) if matches(e, query, cwd)]
    for e in entries:
        indent = "  " * e.depth if mode == "tree" else ""
        name = (str(e.path.relative_to(cwd)) if mode == "recent" else e.name) + ("/" if e.is_dir else "")
        size = "" if e.is_dir and mode != "size" else human(e.size)
        print(f"{indent}{name}\t{size}\t{age(e.mtime)}" if mode in ("detail", "size", "recent") else indent + name)


def smart_target(word: str, cwd: Path):
    """`l WORD`: a real path wins; else a unique child dir by prefix; else a filter."""
    p = Path(os.path.expanduser(word))
    p = p if p.is_absolute() else cwd / p
    if p.is_dir():
        return p.resolve(), ""
    if p.is_file():
        return p.parent.resolve(), p.name
    try:
        dirs = [c for c in cwd.iterdir() if c.is_dir() and c.name.casefold().startswith(word.casefold())]
    except OSError:
        dirs = []
    if len(dirs) == 1:
        return dirs[0].resolve(), ""
    return cwd, word


def main(argv: List[str], mode: str = "detail") -> int:
    args = list(argv)
    handoff = os.environ.get("LOOK_HANDOFF")
    if args and args[0] == "--handoff" and len(args) > 1:
        handoff = args[1]
        args = args[2:]
    cwd = Path.cwd()
    query = ""
    if args:
        cwd, query = smart_target(" ".join(args), cwd)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print_listing(cwd, mode, query)
        return 0

    def go(target: Path):
        if handoff:
            try:
                Path(handoff).write_text(str(target))
            except OSError:
                pass

    filer = Filer(cwd, mode, query, on_go=go)
    filer.run()
    return 0

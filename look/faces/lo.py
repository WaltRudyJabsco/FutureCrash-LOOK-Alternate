"""lo — talk to LO.

    lo                         interactive session in the current directory
    lo "question"              answer, then stay (or exit when piped / with --once)
    lo search "question"       prefer web search for this request
    lo @desk "question"        use a saved inference host for this session
    lo --conservative|--workspace|--power|--unsafe
    lo --image photo.jpg "what is this?"
    lo bg "prompt"             queue unattended background work

Inside a session, lines starting with / are LOOK commands, not prompts:
/help /access /model /think /clear /undo /receipts /memory /context /exit
"""
from __future__ import annotations

import base64
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..kernel.agent import Agent, Turn, UI
from ..kernel.journal import journal
from ..kernel.ollama import Ollama, current_host, ensure_local_server, resolve_host
from ..kernel.policy import DESCRIPTIONS, PROFILES
from ..kernel.results import Result
from ..kernel.tools import Tool
from ..mind import personas
from ..paths import roots
from ..settings import settings
from ..term import feedback, keys
from ..term.ansi import code, enabled, fit, style, term_size, RESET
from ..term.markdown import Stream

STATUS_STYLE = {"ok": "ok", "dispatched": "accent", "denied": "warn", "refused": "bad", "failed": "bad", "cancelled": "muted"}


class TerminalUI(UI):
    """Renders a turn as it happens: rolling thought line, receipts, streamed Markdown."""

    def __init__(self, persona_title: str = "LO"):
        self.tty = sys.stdout.isatty()
        self.mode = settings.get("lo.think_display")
        self.md: Optional[Stream] = None
        self.thought_tail = ""
        self.thinking_shown = False
        self.spinner: Optional[_Spinner] = None
        self.title = persona_title
        self.lock = threading.Lock()

    # ---- streaming ----------------------------------------------------------
    def round_start(self, round_no: int) -> None:
        if self.tty and not self.spinner and self.mode != "full":
            self.spinner = _Spinner("thinking" if round_no == 0 else "continuing")
            self.spinner.start()

    def _stop_spinner(self):
        if self.spinner:
            self.spinner.stop()
            self.spinner = None

    def thinking(self, text: str) -> None:
        if self.mode == "quiet":
            return
        if self.mode == "full":
            self._stop_spinner()
            if not self.thinking_shown:
                sys.stdout.write(style("  thinking\n", "dark"))
                self.thinking_shown = True
            sys.stdout.write(style(text, "dark"))
            sys.stdout.flush()
            return
        self.thought_tail = (self.thought_tail + text).replace("\n", " ")[-400:]
        if self.spinner:
            self.spinner.label = "thinking · " + " ".join(self.thought_tail.split())[-(term_size()[0] - 22):]

    def token(self, text: str) -> None:
        self._stop_spinner()
        if self.md is None:
            if self.thinking_shown:
                sys.stdout.write("\n")
                self.thinking_shown = False
            sys.stdout.write(style(f"{self.title.lower()} ›", "accent") + "\n" if self.tty else "")
            self.md = Stream(indent="  " if self.tty else "")
        self.md.feed(text)

    def end_text(self) -> None:
        self._stop_spinner()
        if self.md:
            self.md.finish()
            self.md = None

    # ---- tools -------------------------------------------------------------
    def tool_start(self, tool: Tool, args: Dict) -> None:
        self.end_text()
        head = tool.headline(args)
        if self.tty:
            self.spinner = _Spinner(f"{tool.label} › {head}")
            self.spinner.start()
        else:
            print(f"  {tool.label} › {head}")

    def receipt(self, tool: Tool, result: Result, elapsed: float) -> None:
        self._stop_spinner()
        extra = style(f"  {elapsed:.1f}s", "dark") if elapsed >= 1.0 else ""
        line = f"  {style(result.mark, STATUS_STYLE[result.status])} {style(tool.label, 'muted')} {result.receipt}{extra}"
        print(fit(line, term_size()[0] - 1, "…").rstrip() if self.tty else line)
        if result.status in ("ok", "dispatched"):
            feedback.play("accept")

    def notice(self, text: str) -> None:
        self._stop_spinner()
        print(style(f"  · {text}", "muted"))

    def ask(self, prompt: str, choices: Dict[str, str], default: str) -> str:
        self._stop_spinner()
        self.end_text()
        if not (sys.stdin.isatty() and self.tty):
            return default
        feedback.play("notify")
        print()
        for i, line in enumerate(prompt.splitlines()):
            print((style("  ? ", "warn") + style(line, "bold")) if i == 0 else "  " + line)
        print("    " + "   ".join(f"{style('[' + k + ']', 'accent')} {v}" for k, v in choices.items()))
        sys.stdout.write(style("    › ", "warn"))
        sys.stdout.flush()
        with keys.raw():
            while True:
                key = keys.read_key()
                if key in ("ctrl-c", "esc"):
                    answer = default
                    break
                if key and key.lower() in choices:
                    answer = key.lower()
                    break
                if key == "enter":
                    answer = default
                    break
        print(choices.get(answer, answer))
        return answer


class _Spinner:
    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.started = time.monotonic()

    def start(self):
        if feedback.motion() == "off":
            sys.stdout.write(style(f"  {self.label}\r", "dark"))
            sys.stdout.flush()
            return
        self._thread.start()

    def _run(self):
        i = 0
        while not self._stop.wait(0.09):
            cols = term_size()[0] - 1
            secs = time.monotonic() - self.started
            frame = feedback.SPINNER[i % len(feedback.SPINNER)]
            sys.stdout.write("\r" + fit(style(f"  {frame} {self.label}", "dark") + (style(f" {secs:.0f}s", "dark") if secs > 2 else ""), cols) + "\r")
            sys.stdout.flush()
            i += 1

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(0.3)
        sys.stdout.write("\r" + " " * (term_size()[0] - 1) + "\r")
        sys.stdout.flush()


# ---- session -------------------------------------------------------------

class Session:
    def __init__(self, agent: Agent, host_name: str, force_search: bool = False):
        self.agent = agent
        self.host_name = host_name
        self.ui: TerminalUI = agent.ui  # type: ignore[assignment]
        self.force_search = force_search

    def header(self) -> None:
        a = self.agent
        alive = style("●", "ok")
        caps = a.client.capabilities(a.model) or []
        bits = [style(a.persona.title, "title"), a.model, f"{self.host_name} {alive}", style(a.access.upper(), "warn" if a.access in ("power", "unsafe") else "muted"),
                style(f"{settings.get('lo.thinking')}/{settings.get('lo.think_display')}", "muted")]
        print(" · ".join(bits))
        print(style(str(a.ctx.workspace), "path"))
        extras = []
        if a.ctx.selected:
            extras.append(f"context · {len(a.ctx.selected)} selected")
        if "vision" in caps:
            extras.append("vision")
        if not a.tools_supported:
            extras.append(style("this model cannot use tools", "warn"))
        extras.append("/help · ctrl-d exit · ctrl-c cancel")
        print(style(" · ".join(extras), "dark"))
        print(style("─" * min(term_size()[0] - 1, 72), "dark"))

    def run_turn(self, text: str, images: Optional[List[str]] = None) -> Turn:
        prompt = text
        if self.force_search:
            prompt = text + "\n\n(Use web_search for this request.)"
        worker_result: Dict[str, Turn] = {}

        def work():
            worker_result["turn"] = self.agent.turn(prompt, images)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        try:
            while t.is_alive():
                t.join(0.1)
        except KeyboardInterrupt:
            self.agent.cancel.set()
            t.join(5)
        self.ui.end_text()
        turn = worker_result.get("turn") or Turn(prompt=text, cancelled=True)
        rate = f" · {turn.output_tokens / turn.seconds:.0f} tok/s" if turn.seconds and turn.output_tokens else ""
        show = settings.get("lo.ledger") == "always" or turn.results or turn.error or turn.cancelled
        if show:
            color = "bad" if turn.error else "muted"
            print(style(f"  {turn.ledger()}", color) + style(f" · {turn.seconds:.1f}s{rate}", "dark"))
        feedback.play("error" if turn.error else "complete")
        print()
        return turn

    def slash(self, line: str) -> bool:
        """Returns False to exit the session."""
        parts = line[1:].split()
        cmd, args = (parts[0].lower(), parts[1:]) if parts else ("help", [])
        a = self.agent
        if cmd in {"exit", "quit", "q"}:
            return False
        if cmd == "help":
            print(__doc__.split("Inside a session", 1)[1].strip() if "Inside a session" in __doc__ else __doc__)
            print(style("  /access [conservative|workspace|power|unsafe]  /model NAME  /think light|adaptive|deep\n"
                        "  /clear forget this session's conversation   /undo   /receipts   /memory   /context", "muted"))
        elif cmd == "access":
            if not args:
                for name in PROFILES:
                    mark = style("●", "ok") if name == a.access else " "
                    print(f"  {mark} {name:<13} {style(DESCRIPTIONS[name], 'muted')}")
            elif args[0] in PROFILES:
                if args[0] == "unsafe" and not _confirm_unsafe():
                    return True
                a.access = a.ctx.access = args[0]
                print(style(f"  access for this session: {args[0]}", "ok"))
            else:
                print(style("  unknown profile", "bad"))
        elif cmd == "model":
            if args:
                names = a.client.model_names()
                match = next((n for n in names if n == args[0]), None) or next((n for n in names if n.startswith(args[0])), None)
                if match:
                    a.model = match
                    a._tools_supported = None
                    print(style(f"  model for this session: {match}", "ok"))
                else:
                    print(style(f"  {args[0]} is not installed on {self.host_name}", "bad"))
            else:
                print(f"  {a.model}")
        elif cmd in {"think", "thinking"} and args:
            try:
                settings.set("lo.thinking", args[0])
                print(style(f"  thinking: {args[0]}", "ok"))
            except ValueError as exc:
                print(style(f"  {exc}", "bad"))
        elif cmd == "clear":
            a.history.clear()
            print(style("  conversation cleared (memory untouched)", "muted"))
        elif cmd == "undo":
            r = journal.undo()
            print(f"  {style(r.mark, STATUS_STYLE[r.status])} {r.receipt}")
        elif cmd in {"receipts", "journal"}:
            from .cli import print_journal
            print_journal(int(args[0]) if args and args[0].isdigit() else 12)
        elif cmd == "memory":
            from .cli import show_memory
            show_memory(a.persona.mind)
        elif cmd == "context":
            if not a.ctx.selected:
                print(style("  no selected paths", "muted"))
            for p in a.ctx.selected:
                print("  " + str(p))
        else:
            print(style(f"  unknown command /{cmd} — try /help", "bad"))
        return True

    def loop(self) -> int:
        try:
            import readline  # noqa: F401  (line editing + history for input())
            hist = roots.runtime / "lo_history"
            try:
                readline.read_history_file(str(hist))
            except OSError:
                pass
            readline.set_history_length(500)
        except ImportError:
            readline = None  # type: ignore
            hist = None
        prompt = (code("prompt") + "you › " + RESET) if enabled() else "you › "
        try:
            while True:
                try:
                    line = input(prompt)
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if not self.slash(text):
                        break
                    continue
                self.run_turn(text)
        finally:
            if readline and hist:
                try:
                    readline.write_history_file(str(hist))
                except OSError:
                    pass
        return 0


def _confirm_unsafe() -> bool:
    print(style("  UNSAFE runs shell commands with your full privileges and does not ask first.", "bad"))
    try:
        return input("  Enter unsafe mode for this session? [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def main(argv: List[str], selected: Optional[List[str]] = None, persona_name: str = "lo") -> int:
    args = list(argv)
    access = None
    host_name, host_url = current_host()
    once = False
    images: List[str] = []
    force_search = False
    while args:
        head = args[0]
        if head in ("--conservative", "--workspace", "--power", "--unsafe"):
            access = head[2:]
        elif head in ("--once", "-1"):
            once = True
        elif head == "--image" and len(args) > 1:
            args.pop(0)
            try:
                images.append(base64.b64encode(Path(args[0]).expanduser().read_bytes()).decode())
            except OSError as exc:
                print(style(f"lo · cannot read image: {exc}", "bad"))
                return 1
        elif head.startswith("@") and len(head) > 1:
            host_name, host_url = resolve_host(head[1:])
            if not host_url:
                print(style(f"lo · no saved host named {head[1:]} (lk hosts)", "bad"))
                return 1
        elif head in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            break
        args.pop(0)
    if args and args[0] == "bg":
        prompt = " ".join(args[1:]).strip()
        if not prompt:
            print("usage: lo bg PROMPT")
            return 2
        from ..daemon import jobs
        jid = jobs.enqueue("job", {"prompt": prompt, "access": access or settings.get("lo.access"),
                                   "workspace": str(Path.cwd()), "owner": "lo", "host": host_url})
        print(style(f"queued background job {jid} · results appear in `lk jobs`", "ok"))
        return 0
    if args and args[0] == "search":
        force_search = True
        args.pop(0)

    personas.ensure_profile()
    access = access or settings.get("lo.access")
    if access == "unsafe" and "--unsafe" in argv and sys.stdin.isatty() and not _confirm_unsafe():
        return 0
    client = Ollama(host_url)
    if not ensure_local_server(client):
        print(style(f"lo · Ollama is not reachable at {client.base}", "bad"))
        print(style("  start it with `ollama serve`, or pick a host: lk hosts", "muted"))
        return 1
    model = settings.get("ai.model")
    try:
        installed = client.model_names()
    except Exception as exc:
        print(style(f"lo · {exc}", "bad"))
        return 1
    if model not in installed:
        if not installed:
            print(style(f"lo · {host_name} has no models. Try: ollama pull qwen3:8b", "bad"))
            return 1
        print(style(f"lo · {model} is not installed on {host_name}; choose one with `lk models`", "bad"))
        return 1

    persona = personas.get(persona_name) or personas.LO
    ui = TerminalUI(persona.title)
    agent = Agent(persona, client=client, model=model, access=access, ui=ui, selected=[Path(p) for p in (selected or [])])
    session = Session(agent, host_name, force_search)
    first = " ".join(args).strip()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not first and not sys.stdin.isatty():
        first = sys.stdin.read().strip()
    if interactive:
        session.header()
    if first:
        turn = session.run_turn(first, images or None)
        if once or not interactive:
            return 1 if turn.error else 0
    if not interactive:
        return 0
    return session.loop()

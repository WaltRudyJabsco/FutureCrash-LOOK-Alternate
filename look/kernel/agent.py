"""The agent loop. One of it, for every face.

    turn(text)
      ├─ assemble system prompt (persona · contract · access · craft · memory · environment)
      ├─ round: stream from Ollama → thinking / tokens / tool calls
      │    └─ each tool call → invoke() → Result → host renders the receipt → result returns to the model
      ├─ budget grows while rounds make progress, up to lo.max_rounds
      └─ ledger: what this turn actually did, computed from Results, never from prose

Faces supply a UI object; the loop never prints.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..log import log
from ..settings import settings
from . import lane, prompts
from .ollama import Ollama, OllamaError
from .policy import capabilities
from .results import Result
from .tools import CHANGE, DISPATCH, EXECUTE, Context, Tool, invoke, load_builtin_tools

INITIAL_ROUNDS = 4
ROUND_STEP = 4
CONTEXT_SIZES = (8192, 16384, 32768)
HISTORY_CHARS = 36000
OLD_TOOL_CHARS = 700


class UI:
    """Default no-op UI. Faces override what they render."""

    def thinking(self, text: str) -> None: ...
    def token(self, text: str) -> None: ...
    def round_start(self, round_no: int) -> None: ...
    def tool_start(self, tool: Tool, args: Dict[str, Any]) -> None: ...
    def receipt(self, tool: Tool, result: Result, elapsed: float) -> None: ...
    def notice(self, text: str) -> None: ...

    def ask(self, prompt: str, choices: Dict[str, str], default: str) -> str:
        return default


@dataclass
class Turn:
    prompt: str
    answer: str = ""
    thinking: str = ""
    results: List[Tuple[str, str, Result]] = field(default_factory=list)  # (tool, effect, result)
    rounds: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cancelled: bool = False
    error: str = ""

    @property
    def changes(self) -> int:
        return sum(r.changes for _, effect, r in self.results if effect == CHANGE and r.ok)

    def ledger(self) -> str:
        """A language-independent account of what happened. Faces print it as-is."""
        if self.cancelled:
            return "· cancelled"
        if self.error:
            return f"✗ {self.error}"
        if not self.results:
            return "◦ no tools used"
        ok_changes = self.changes
        dispatched = sum(1 for _, e, r in self.results if e == DISPATCH and r.ok)
        commands = sum(1 for _, e, r in self.results if e == EXECUTE and r.status in ("ok", "failed") and "exit_code" in r.data)
        observed = sum(1 for _, e, r in self.results if e not in (CHANGE, DISPATCH, EXECUTE) and r.ok)
        blocked = sum(1 for _, e, r in self.results if not r.ok and not (e == EXECUTE and "exit_code" in r.data))
        bits = []
        if ok_changes:
            bits.append(f"✓ {ok_changes} change{'s' if ok_changes != 1 else ''} · lk undo")
        if dispatched:
            bits.append(f"↗ {dispatched} handed off")
        if commands:
            bits.append(f"$ {commands} command{'s' if commands != 1 else ''} ran")
        if observed:
            bits.append(f"{observed} look{'s' if observed != 1 else ''}")
        if blocked:
            bits.append(f"✗ {blocked} did not happen")
        if not ok_changes and any(e == CHANGE for _, e, _ in self.results):
            bits.append("no files changed")
        return " · ".join(bits)


def estimate_tokens(messages: List[dict], tools: Optional[List[dict]]) -> int:
    chars = sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
    chars += len(json.dumps(tools)) if tools else 0
    return int(chars / 3.2)


class Agent:
    def __init__(self, persona, client: Optional[Ollama] = None, model: Optional[str] = None,
                 access: Optional[str] = None, workspace: Optional[Path] = None, ui: Optional[UI] = None,
                 interactive: bool = True, selected: Optional[List[Path]] = None,
                 lane_client: Optional[str] = None, extra_system: str = "", remember: bool = True,
                 lease: bool = True, tool_filter: Optional[set] = None):
        load_builtin_tools()
        self.persona = persona
        self.client = client or Ollama()
        self.model = model or settings.get("ai.model")
        self.access = access or settings.get("lo.access")
        self.ui = ui or UI()
        self.ctx = Context(workspace=(workspace or Path.cwd()).resolve(), access=self.access, actor=persona.name,
                           ui=self.ui, interactive=interactive, selected=[Path(p).resolve() for p in (selected or [])])
        self.history: List[dict] = []
        self.lane_client = lane_client or persona.name
        self.extra_system = extra_system
        self.remember = remember
        self.lease = lease              # False for ambient work that must not block background jobs
        self.tool_filter = tool_filter  # restrict offered tools by name (e.g. ambient: signal only)
        self.cancel = threading.Event()
        self._tools_supported: Optional[bool] = None

    # ---- capabilities ----------------------------------------------------
    @property
    def tools_supported(self) -> bool:
        if self._tools_supported is None:
            self._tools_supported = self.client.supports(self.model, "tools")
        return self._tools_supported

    def offered_tools(self) -> List[Tool]:
        from .tools import registry
        if not self.tools_supported:
            return []
        tools = registry.offered(capabilities(self.access), self.persona.name)
        return [t for t in tools if t.name in self.tool_filter] if self.tool_filter is not None else tools

    def think_for(self, turn: Turn, round_no: int, last_failed: bool) -> Optional[bool]:
        if not self.client.supports(self.model, "thinking"):
            return None
        mode = settings.get("lo.thinking")
        if mode == "light":
            return False
        if mode == "deep":
            return True
        # adaptive: structural signals only — no keyword lists.
        text = turn.prompt
        if round_no > 0 and last_failed:
            return True
        return len(text) > 400 or "```" in text or "\n" in text.strip() or len(self.ctx.selected) >= 3

    # ---- history ---------------------------------------------------------
    def _trimmed_history(self) -> List[dict]:
        msgs = [dict(m) for m in self.history]
        # Older tool payloads shrink; the latest turn keeps full detail.
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=0)
        for i, m in enumerate(msgs[:last_user]):
            if m.get("role") == "tool" and len(str(m.get("content", ""))) > OLD_TOOL_CHARS:
                m["content"] = str(m["content"])[:OLD_TOOL_CHARS] + " …"
        total = 0
        kept: List[dict] = []
        for m in reversed(msgs):
            size = len(json.dumps(m, ensure_ascii=False, default=str))
            if kept and total + size > HISTORY_CHARS:
                break
            kept.append(m)
            total += size
        kept.reverse()
        while kept and kept[0].get("role") != "user":
            kept.pop(0)  # never start mid-transaction
        return kept

    # ---- the turn --------------------------------------------------------
    def turn(self, text: str, images: Optional[List[str]] = None) -> Turn:
        turn = Turn(prompt=text)
        started = time.monotonic()
        self.cancel.clear()
        system = prompts.assemble(self.persona, self.access, self.ctx.workspace, text, self.tools_supported,
                                  self.ctx.selected, memory_enabled=settings.get("memory.enabled") and self.remember,
                                  extra=self.extra_system)
        user_msg: Dict[str, Any] = {"role": "user", "content": text}
        if images:
            user_msg["images"] = images
        work: List[dict] = [user_msg]
        budget = INITIAL_ROUNDS
        ceiling = settings.get("lo.max_rounds")
        signatures: Dict[str, int] = {}
        last_failed = False

        try:
            with (lane.interactive(self.lane_client) if self.lease else _null()):
                round_no = 0
                while True:
                    final_round = round_no >= min(budget, ceiling)
                    tools = None if final_round else [t.schema() for t in self.offered_tools()] or None
                    messages = [{"role": "system", "content": system}] + self._trimmed_history() + work
                    if final_round and turn.results:
                        messages.append({"role": "system", "content": "Tool budget for this turn is spent. Answer now from the results you have, and say what is left undone."})
                    need = estimate_tokens(messages, tools) + 3000
                    num_ctx = next((n for n in CONTEXT_SIZES if n >= need), CONTEXT_SIZES[-1])
                    self.ui.round_start(round_no)
                    content, thinking, calls, stats = self._stream(messages, tools, self.think_for(turn, round_no, last_failed), num_ctx)
                    turn.rounds += 1
                    turn.prompt_tokens += stats.get("prompt_eval_count", 0)
                    turn.output_tokens += stats.get("eval_count", 0)
                    turn.thinking += thinking
                    if self.cancel.is_set():
                        turn.cancelled = True
                        break
                    if not calls or final_round:
                        # On the final round tools were not offered; stray calls are ignored.
                        turn.answer = content
                        break
                    work.append({"role": "assistant", "content": content, "tool_calls": calls})
                    progress = False
                    last_failed = False
                    for call in calls:
                        name, args = _parse_call(call)
                        result, effect = self._run_call(name, args, signatures)
                        if result is None:
                            continue
                        turn.results.append((name, effect, result))
                        work.append({"role": "tool", "tool_name": name, "content": result.for_model()})
                        if result.ok and signatures.get(_signature(name, args), 0) == 1:
                            progress = True
                        if not result.ok:
                            last_failed = True
                        if self.cancel.is_set():
                            break
                    if self.cancel.is_set():
                        turn.cancelled = True
                        break
                    round_no += 1
                    if round_no >= budget and progress and budget < ceiling:
                        budget = min(ceiling, budget + ROUND_STEP)
        except KeyboardInterrupt:
            turn.cancelled = True
        except OllamaError as exc:
            turn.error = str(exc)
        turn.seconds = time.monotonic() - started

        if turn.answer or turn.results:
            self.history.extend([user_msg if not images else {"role": "user", "content": text}] + work[1:])
            self.history.append({"role": "assistant", "content": turn.answer})
        if turn.answer and not turn.cancelled and self.remember:
            self._after_turn(turn)
        return turn

    def _stream(self, messages, tools, think, num_ctx) -> Tuple[str, str, List[dict], Dict[str, int]]:
        content, thinking, calls = [], [], []
        stats: Dict[str, int] = {}
        options = {"num_ctx": num_ctx, "temperature": 0.35}
        stream = self.client.chat_stream(self.model, messages, tools=tools, think=think, options=options)
        router = _ThinkRouter(lambda s: (content.append(s), self.ui.token(s)),
                              lambda s: (thinking.append(s), self.ui.thinking(s)))
        try:
            for event in stream:
                if self.cancel.is_set():
                    break
                msg = event.get("message") or {}
                if msg.get("thinking"):
                    thinking.append(msg["thinking"])
                    self.ui.thinking(msg["thinking"])
                if msg.get("content"):
                    router.feed(msg["content"])
                if msg.get("tool_calls"):
                    calls.extend(msg["tool_calls"])
                if event.get("done"):
                    stats = {k: int(event.get(k) or 0) for k in ("prompt_eval_count", "eval_count")}
        finally:
            router.flush()
            close = getattr(stream, "close", None)
            if close:
                close()
        return "".join(content).strip(), "".join(thinking), calls, stats

    def _run_call(self, name: str, args: Dict[str, Any], signatures: Dict[str, int]) -> Tuple[Optional[Result], str]:
        from .tools import registry
        tool = registry.get(name)
        sig = _signature(name, args)
        signatures[sig] = signatures.get(sig, 0) + 1
        if tool is None:
            return Result.refused(f"there is no tool named {name!r}"), "observe"
        if tool not in self.offered_tools():
            result = Result.denied(f"{name} is not available in {self.access} access")
            self.ui.receipt(tool, result, 0.0)
            return result, tool.effect
        if signatures[sig] > 2:
            result = Result.refused(f"{name} was already called twice with these exact arguments; use the earlier result or try something different")
            return result, tool.effect
        self.ui.tool_start(tool, args)
        result, elapsed = invoke(tool, self.ctx, args)
        self.ui.receipt(tool, result, elapsed)
        return result, tool.effect

    def _after_turn(self, turn: Turn) -> None:
        """Commit literal continuity now; queue semantic reflection for the background lane."""
        if not settings.get("memory.enabled"):
            return
        try:
            mind = self.persona.mind
            mind.add_recent(turn.prompt, turn.answer)
            from ..daemon import jobs
            jobs.enqueue("reflect", {"mind": mind.name, "persona": self.persona.name, "access": self.access,
                                     "user": turn.prompt, "assistant": turn.answer, "model": self.model,
                                     "host": self.client.base})
        except Exception as exc:  # boundary: memory is best-effort and must never break a turn
            log.error("agent.after_turn", exc)


class _ThinkRouter:
    """Streams content, diverting inline <think>…</think> to the thinking channel even
    when a tag is split across chunks."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, on_text, on_thought):
        self.on_text, self.on_thought = on_text, on_thought
        self.buf = ""
        self.inside = False

    def feed(self, piece: str) -> None:
        self.buf += piece
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.lower().find(tag)
            if idx >= 0:
                self._emit(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # hold back a tail that could be the start of the tag
            hold = next((n for n in range(min(len(tag) - 1, len(self.buf)), 0, -1)
                         if tag.startswith(self.buf[-n:].lower())), 0)
            self._emit(self.buf[:len(self.buf) - hold])
            self.buf = self.buf[len(self.buf) - hold:]
            return

    def _emit(self, text: str) -> None:
        if text:
            (self.on_thought if self.inside else self.on_text)(text)

    def flush(self) -> None:
        self._emit(self.buf)
        self.buf = ""


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _parse_call(call: dict) -> Tuple[str, Dict[str, Any]]:
    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return name, args if isinstance(args, dict) else {}


def _signature(name: str, args: Dict[str, Any]) -> str:
    return name + ":" + json.dumps(args, sort_keys=True, default=str)

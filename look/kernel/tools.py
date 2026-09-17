"""Tools are declarations, not branches.

Each tool states what it needs (a capability), what kind of effect it has, and
how it is labelled in the terminal. The agent loop, the permission table, the
journal and the `lk tools` listing all read these declarations. Adding a tool
never means editing the loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ..log import log
from .results import Result

# Capabilities — what a tool may touch.
READ = "read"      # local files and host inspection, read-only
NET = "net"        # outbound information edges and web search
DRAW = "draw"      # the Signal canvas
WRITE = "write"    # create/modify/move/remove files (journaled, undoable)
OPEN = "open"      # hand things to desktop apps / browser
TASKS = "tasks"    # schedule recurring work
EXEC = "exec"      # arbitrary shell commands
CAPABILITIES = (READ, NET, DRAW, WRITE, OPEN, TASKS, EXEC)

# Effects — what kind of fact a successful call establishes.
OBSERVE = "observe"    # nothing changed
CHANGE = "change"      # local state changed (journaled)
DISPATCH = "dispatch"  # something was handed to another program
EXECUTE = "execute"    # a shell command ran (not journal-reversible)


class Param:
    def __init__(self, type_: str, description: str, required: bool = True, enum: Optional[List[str]] = None,
                 items: Optional[dict] = None):
        self.type = type_
        self.description = description
        self.required = required
        self.enum = enum
        self.items = items

    def schema(self) -> dict:
        out: Dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            out["enum"] = self.enum
        if self.items:
            out["items"] = self.items
        return out


@dataclass
class Tool:
    name: str
    description: str
    capability: str
    effect: str
    label: str
    params: Dict[str, Param]
    fn: Callable[..., Result]
    personas: Optional[Set[str]] = None  # None = every persona
    available: Optional[Callable[[], bool]] = None  # e.g. web search only with a key

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {k: p.schema() for k, p in self.params.items()},
                    "required": [k for k, p in self.params.items() if p.required],
                },
            },
        }

    def headline(self, args: Dict[str, Any]) -> str:
        """Short argument echo for the activity line: `read ›  notes/todo.md`."""
        for key in ("path", "paths", "query", "location", "url", "command", "title", "source", "prompt"):
            if key in args and args[key]:
                value = args[key]
                if isinstance(value, list):
                    value = f"{value[0]} (+{len(value) - 1})" if len(value) > 1 else (value[0] if value else "")
                return str(value)[:120]
        if "files" in args and isinstance(args["files"], list):
            return f"{len(args['files'])} files"
        return ""


class Registry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def tool(self, name: str, description: str, capability: str, effect: str = OBSERVE, label: Optional[str] = None,
             params: Optional[Dict[str, Param]] = None, personas: Optional[Iterable[str]] = None,
             available: Optional[Callable[[], bool]] = None):
        if capability not in CAPABILITIES:
            raise ValueError(f"{name}: unknown capability {capability}")

        def wrap(fn: Callable[..., Result]) -> Callable[..., Result]:
            self._tools[name] = Tool(name, description, capability, effect, label or name.replace("_", " "),
                                     params or {}, fn, set(personas) if personas else None, available)
            return fn
        return wrap

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def offered(self, capabilities: Set[str], persona: str) -> List[Tool]:
        return [t for t in self._tools.values()
                if t.capability in capabilities and (t.personas is None or persona in t.personas)
                and (t.available is None or t.available())]


registry = Registry()


def load_builtin_tools() -> Registry:
    """Import tool modules for their registration side effect (idempotent)."""
    from ..tools import files, host, inspect, edges, shell, signal, tasks  # noqa: F401
    from .. import extras
    extras.load()
    return registry


@dataclass
class Context:
    """Everything a tool may rely on during one call."""
    workspace: Path
    access: str
    actor: str = "lo"
    ui: Any = None               # object with ask(prompt, choices) -> str and notice(text)
    interactive: bool = True
    selected: List[Path] = field(default_factory=list)
    session_grants: Set[Tuple[str, str]] = field(default_factory=set)
    extras: Dict[str, Any] = field(default_factory=dict)

    def ask(self, prompt: str, choices: Dict[str, str], default: str) -> str:
        if not self.interactive or self.ui is None:
            return default
        try:
            return self.ui.ask(prompt, choices, default)
        except (EOFError, KeyboardInterrupt):
            return default


def invoke(tool: Tool, ctx: Context, args: Dict[str, Any]) -> Tuple[Result, float]:
    """The single transaction boundary: policy → run → classify → journal."""
    from .policy import allowed
    from .journal import journal

    from .fsops import BASE
    token = BASE.set(ctx.workspace)
    started = time.monotonic()
    if not isinstance(args, dict):
        args = {}
    if not allowed(ctx.access, tool.capability):
        result = Result.denied(f"{tool.name} needs {tool.capability} access; this session is {ctx.access}")
    else:
        unknown = set(args) - set(tool.params)
        missing = [k for k, p in tool.params.items() if p.required and args.get(k) in (None, "", [])]
        if missing:
            result = Result.refused(f"{tool.name}: missing {', '.join(missing)}")
        else:
            clean = {k: v for k, v in args.items() if k not in unknown}
            try:
                result = tool.fn(ctx, **clean)
                if not isinstance(result, Result):
                    raise TypeError(f"{tool.name} returned {type(result).__name__}, not Result")
            except KeyboardInterrupt:
                result = Result.cancelled(f"{tool.name} cancelled")
            except Exception as exc:  # boundary: a tool bug must not kill the session
                log.error(f"tool.{tool.name}", exc)
                result = Result.failed(f"{tool.name} failed: {exc}")
    elapsed = time.monotonic() - started
    BASE.reset(token)
    journal.record_tool(ctx.actor, tool, args, result, elapsed)
    return result, elapsed

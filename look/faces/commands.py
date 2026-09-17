"""The lk command registry.

One declaration per command gives dispatch, `lk help`, the `lk commands`
palette, shell completion and docs/COMMANDS.md. Aliases point at the same
function; there are no parallel implementations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple


@dataclass
class Command:
    path: Tuple[str, ...]           # ("memory", "forget")
    fn: Callable[[List[str]], int]
    usage: str
    summary: str
    area: str
    aliases: Tuple[Tuple[str, ...], ...] = ()
    hidden: bool = False
    complete: Optional[Callable[[], List[str]]] = None

    @property
    def name(self) -> str:
        return " ".join(self.path)


AREAS = [("files", "Files"), ("ai", "LO & models"), ("mind", "Memory & craft"), ("system", "Machine"),
         ("net", "Network"), ("time", "Schedules & jobs"), ("config", "Settings & profile"), ("meta", "LOOK itself"),
         ("extra", "Extras")]


class Commands:
    def __init__(self):
        self._by_path: Dict[Tuple[str, ...], Command] = {}
        self._ordered: List[Command] = []

    def command(self, name: str, usage: str = "", summary: str = "", area: str = "meta",
                aliases: Sequence[str] = (), hidden: bool = False, complete: Optional[Callable[[], List[str]]] = None):
        path = tuple(name.split())

        def wrap(fn):
            cmd = Command(path, fn, usage, summary, area, tuple(tuple(a.split()) for a in aliases), hidden, complete)
            self._by_path[path] = cmd
            for a in cmd.aliases:
                self._by_path[a] = cmd
            self._ordered.append(cmd)
            return fn
        return wrap

    def resolve(self, argv: List[str]) -> Tuple[Optional[Command], List[str]]:
        """Longest-prefix match: `lk memory forget x` → ("memory","forget"), ["x"]."""
        for n in range(min(len(argv), 3), 0, -1):
            cmd = self._by_path.get(tuple(argv[:n]))
            if cmd:
                return cmd, argv[n:]
        return None, argv

    def all(self, include_hidden: bool = False) -> List[Command]:
        return [c for c in self._ordered if include_hidden or not c.hidden]

    def children(self, prefix: Tuple[str, ...]) -> List[Command]:
        return [c for c in self._ordered if c.path[:len(prefix)] == prefix and len(c.path) > len(prefix) and not c.hidden]

    def words(self, prefix: Sequence[str] = ()) -> List[str]:
        prefix = tuple(prefix)
        out = []
        for key in self._by_path:
            if key[:len(prefix)] == prefix and len(key) > len(prefix):
                out.append(key[len(prefix)])
        return sorted(set(out))


commands = Commands()

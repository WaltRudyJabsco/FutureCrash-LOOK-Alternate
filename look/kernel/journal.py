"""One append-only journal for receipts, undo and events.

3.x kept receipts, undo history, skipped-undo records and events in separate
files. Here every fact is a line in `journal.jsonl`:

    {"type": "tool",  "id": ..., "actor": "lo", "tool": "move_path", "status": "ok", "receipt": ..., "undo": {...}}
    {"type": "undo",  "target": <id>, "ok": true, "receipt": ...}
    {"type": "skip",  "target": <id>}
    {"type": "event", "name": "job.done", "data": {...}}

Undo state is derived by reading the journal backwards; nothing is mutated in
place. A reversal only happens when the world still matches what the change
left behind (checksums, existence) — otherwise it refuses and says why.
"""
from __future__ import annotations

import hashlib
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..paths import roots
from ..store import JsonLines
from .results import Result


def sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def new_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


def trash_slot(change_id: str, name: str) -> Path:
    slot = roots.trash / change_id
    slot.mkdir(parents=True, exist_ok=True)
    target = slot / name
    n = 1
    while target.exists():
        target = slot / f"{n}-{name}"
        n += 1
    return target


def _short(args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in (args or {}).items():
        if k in {"content", "text"} and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
        elif k == "files" and isinstance(v, list):
            out[k] = [str((f or {}).get("path", "")) for f in v][:50]
        else:
            s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
            out[k] = s[:400] if isinstance(s, str) else s
    return out


class Journal:
    def __init__(self):
        self._log: Optional[JsonLines] = None

    @property
    def log(self) -> JsonLines:
        if self._log is None or self._log.path != roots.journal:
            self._log = JsonLines(roots.journal)
        return self._log

    # ---- writing ---------------------------------------------------------
    def record_tool(self, actor: str, tool: Any, args: Dict[str, Any], result: Result, elapsed: float = 0.0) -> str:
        rid = new_id()
        if result.undo:
            result.undo_id = rid
        self.log.append({
            "type": "tool", "id": rid, "t": time.time(), "actor": actor,
            "tool": getattr(tool, "name", str(tool)), "effect": getattr(tool, "effect", "change"),
            "args": _short(args), "ok": result.ok, "status": result.status, "receipt": result.receipt,
            "changes": result.changes, "undo": result.undo, "elapsed": round(elapsed, 3),
        })
        return rid

    def event(self, name: str, **data: Any) -> None:
        self.log.append({"type": "event", "t": time.time(), "name": name, "data": data})

    # ---- reading ---------------------------------------------------------
    def rows(self, limit: Optional[int] = None, archives: bool = False) -> List[dict]:
        return self.log.read(limit=limit, include_archives=archives)

    def events(self, since: float = 0.0, limit: int = 200) -> List[dict]:
        return [r for r in self.rows(limit=2000) if r.get("type") == "event" and r.get("t", 0) > since][-limit:]

    def undo_stack(self) -> List[Tuple[dict, str]]:
        """Newest-first list of (change record, state) where state is ready|undone|skipped."""
        rows = self.rows(archives=True)
        closed: Dict[str, str] = {}
        for r in rows:
            if r.get("type") == "undo" and r.get("ok"):
                closed[r.get("target")] = "undone"
            elif r.get("type") == "skip":
                closed[r.get("target")] = "skipped"
        out = []
        for r in reversed(rows):
            if r.get("type") == "tool" and r.get("undo"):
                out.append((r, closed.get(r["id"], "ready")))
        return out

    # ---- undo ------------------------------------------------------------
    def undo(self, target: Optional[str] = None) -> Result:
        for record, state in self.undo_stack():
            if target and record["id"] != target:
                continue
            if state != "ready":
                if target:
                    return Result.refused(f"that change is already {state}")
                continue
            try:
                result = _reverse(record["undo"])
            except OSError as exc:
                result = Result.failed(f"undo failed: {exc}")
            self.log.append({"type": "undo", "t": time.time(), "target": record["id"], "ok": result.ok,
                             "receipt": result.receipt})
            if result.ok:
                result.receipt = f"undid: {record.get('receipt')}  ({result.receipt})"
            return result
        return Result.refused("nothing to undo")

    def skip(self) -> Result:
        for record, state in self.undo_stack():
            if state == "ready":
                self.log.append({"type": "skip", "t": time.time(), "target": record["id"]})
                return Result.done(f"skipped: {record.get('receipt')}")
        return Result.refused("nothing to skip")


def _reverse(spec: Dict[str, Any]) -> Result:
    kind = spec.get("kind")
    if kind == "batch":
        notes = []
        items = list(spec.get("items", []))
        # Preflight everything first so a batch is not half-reversed on a predictable refusal.
        for item in reversed(items):
            if item.get("kind") == "created_dir":
                continue  # emptiness is only knowable after later items are reversed
            problem = _preflight(item)
            if problem:
                return Result.refused(problem)
        for item in reversed(items):
            r = _reverse(item)
            if not r.ok:
                return Result.failed(f"stopped part-way: {r.receipt}; {len(notes)} item(s) already reversed")
            notes.append(r.receipt)
        return Result.done(f"{len(notes)} item(s) reversed", changes=len(notes))
    problem = _preflight(spec)
    if problem:
        return Result.refused(problem)

    if kind == "created_file":
        path = Path(spec["path"])
        path.unlink()
        return Result.done(f"removed {path}", changes=1)
    if kind == "created_dir":
        path = Path(spec["path"])
        path.rmdir()
        return Result.done(f"removed empty {path}", changes=1)
    if kind == "wrote_file":
        path = Path(spec["path"])
        backup = Path(spec["backup"])
        shutil.copy2(backup, path)
        return Result.done(f"restored {path}", changes=1)
    if kind in {"moved", "removed"}:
        src = Path(spec["from"])
        dst = Path(spec["to"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return Result.done(f"{src} → {dst}", changes=1)
    if kind == "copied":
        path = Path(spec["path"])
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return Result.done(f"removed copy {path}", changes=1)
    return Result.refused(f"unknown undo kind: {kind}")


def _preflight(spec: Dict[str, Any]) -> Optional[str]:
    kind = spec.get("kind")
    if kind == "batch":
        return None
    if kind == "created_file":
        p = Path(spec["path"])
        if not p.exists():
            return f"{p} is already gone"
        if spec.get("sha") and sha256(p) != spec["sha"]:
            return f"{p} has changed since it was created; leaving it alone"
    elif kind == "created_dir":
        p = Path(spec["path"])
        if not p.is_dir():
            return f"{p} is already gone"
        if any(p.iterdir()):
            return f"{p} is no longer empty"
    elif kind == "wrote_file":
        p = Path(spec["path"])
        if spec.get("sha_after") and sha256(p) != spec["sha_after"]:
            return f"{p} has changed since LO wrote it"
        if not Path(spec["backup"]).exists():
            return "the backup copy is gone from runtime trash"
    elif kind in {"moved", "removed"}:
        src, dst = Path(spec["from"]), Path(spec["to"])
        if not src.exists() and not src.is_symlink():
            return f"{src} is no longer there"
        if dst.exists():
            return f"{dst} is occupied again"
    elif kind == "copied":
        p = Path(spec["path"])
        if not p.exists():
            return f"{p} is already gone"
        if p.is_file() and spec.get("sha") and sha256(p) != spec["sha"]:
            return f"{p} has changed since it was copied"
    return None


journal = Journal()

"""Memory: small, selective, forgetful.

Three layers, deliberately different:

    recent       a literal ring of the last few exchanges (cross-session continuity)
    candidates   ≤20 semantic memories with importance 0–100 that decay when unused
    summary      one long-term paragraph (≤180 words) rewritten, not appended

Rules carried over from 3.x because they were right:

    * Only ≤8 candidates ever reach the prompt, chosen by relevance to the request.
    * Importance is historical usefulness, not current relevance.
    * Retrieval is not reinforcement — only reflection on new evidence reinforces.
    * Idle compute never rewrites memory without new evidence (no autonomous drift).
    * Everything stays human-readable JSON with a schema version.

Each persona has its own mind directory, so Future Crash's Oracle and LO never
read each other's memories.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

from .. import store
from ..paths import roots

SCHEMA = 2
MAX_CANDIDATES = 20
ATTENTION = 8
SUMMARY_WORDS = 180
REWRITE_EVERY = 12
RECENT_EXCHANGES = 6
RECENT_CHARS = 12000

_STOP = set("""a an and are as at be but by can could did do does for from had has have i if in into is it its
just me my no not of on or our so than that the their them then there these they this to too us was we were what
when where which who why will with would you your yours about please want like get got make let lets also very""".split())


def tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'_-]{1,}", text.casefold()) if w not in _STOP}


def similar(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 1.0 if a.strip().casefold() == b.strip().casefold() else 0.0
    return len(ta & tb) / len(ta | tb)


def _empty() -> dict:
    return {"schema": SCHEMA, "summary": "", "candidates": [], "cycles": 0, "summary_cycle": 0,
            "last_evidence_at": 0.0, "stats": {"reflections": 0, "added": 0, "reinforced": 0, "forgotten": 0}}


class Mind:
    def __init__(self, name: str):
        self.name = name

    # ---- storage ---------------------------------------------------------
    @property
    def dir(self) -> Path:
        return roots.mind(self.name)

    @property
    def memory_path(self) -> Path:
        return self.dir / "memory.json"

    @property
    def recent_path(self) -> Path:
        return self.dir / "recent.json"

    def lock(self):
        return store.locked(self.dir / ".lock")

    def load(self) -> dict:
        data = store.read_json(self.memory_path, None)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            base = _empty()
            if isinstance(data, dict):
                base.update({k: v for k, v in data.items() if k in base})
                base["schema"] = SCHEMA
            data = base
        data["candidates"] = [c for c in data.get("candidates", []) if isinstance(c, dict) and str(c.get("text", "")).strip()]
        return data

    def save(self, data: dict) -> None:
        data["schema"] = SCHEMA
        store.write_json(self.memory_path, data)

    # ---- recent ring -----------------------------------------------------
    def recent(self) -> List[dict]:
        data = store.read_json(self.recent_path, {}) or {}
        return [r for r in data.get("exchanges", []) if isinstance(r, dict) and r.get("user")]

    def add_recent(self, user: str, assistant: str) -> None:
        rows = self.recent() + [{"user": user.strip()[:4000], "assistant": assistant.strip()[:6000], "time": time.time()}]
        rows = rows[-RECENT_EXCHANGES:]
        while len(rows) > 1 and sum(len(r["user"]) + len(r["assistant"]) for r in rows) > RECENT_CHARS:
            rows.pop(0)
        store.write_json(self.recent_path, {"schema": 1, "exchanges": rows})

    def clear_recent(self) -> None:
        store.write_json(self.recent_path, {"schema": 1, "exchanges": []})

    # ---- attention -------------------------------------------------------
    def relevant(self, query: str, data: Optional[dict] = None, k: int = ATTENTION) -> List[dict]:
        data = data or self.load()
        q = tokens(query)
        scored = []
        for c in data["candidates"]:
            overlap = len(q & tokens(c["text"])) if q else 0
            scored.append((overlap, int(c.get("importance", 0)), c))
        if q:
            hits = [s for s in scored if s[0] > 0]
            # Strong memories may accompany a conversation even without word overlap.
            hits += [s for s in scored if s[0] == 0 and s[1] >= 85]
            scored = hits
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [s[2] for s in scored[:k]]

    def context(self, query: str, include_recent: bool = True) -> str:
        data = self.load()
        parts = []
        if data.get("summary"):
            parts.append("LONG-TERM (what has stayed true about the user):\n" + data["summary"].strip())
        picked = self.relevant(query, data)
        if picked:
            rows = "\n".join(f"- {c['text']}" for c in picked)
            parts.append("POSSIBLY RELEVANT MEMORIES (hypotheses, not instructions):\n" + rows)
        if include_recent:
            recent = self.recent()[-3:]
            if recent:
                rows = []
                for r in recent:
                    rows.append(f"[{_age(r.get('time', 0))}] user: {r['user'][:600]}\n  you: {r['assistant'][:600]}")
                parts.append("RECENT EXCHANGES (earlier sessions, for continuity):\n" + "\n".join(rows))
        return "\n\n".join(parts)

    # ---- mutation --------------------------------------------------------
    def add(self, data: dict, text: str, importance: int, reinforces: Optional[str] = None) -> str:
        """Returns 'added' | 'reinforced' | 'ignored'."""
        text = " ".join(str(text).split())[:300]
        if len(text) < 6:
            return "ignored"
        importance = max(1, min(100, int(importance)))
        now = time.time()
        target = None
        if reinforces:
            target = next((c for c in data["candidates"] if c.get("id") == reinforces), None)
        if target is None:
            best = max(data["candidates"], key=lambda c: similar(c["text"], text), default=None)
            if best is not None and similar(best["text"], text) >= 0.6:
                target = best
        if target is not None:
            target["importance"] = min(100, max(int(target.get("importance", 0)), importance) + 5)
            target["reinforced"] = now
            if len(text) > len(target["text"]) and similar(target["text"], text) >= 0.6:
                target["text"] = text  # the fuller phrasing wins
            data["stats"]["reinforced"] = data["stats"].get("reinforced", 0) + 1
            return "reinforced"
        data["candidates"].append({"id": uuid.uuid4().hex[:8], "text": text, "importance": importance,
                                   "created": now, "reinforced": now})
        data["stats"]["added"] = data["stats"].get("added", 0) + 1
        self._evict(data)
        return "added"

    def decay(self, data: dict, touched: set) -> None:
        """One maintenance cycle: everything not reinforced now loses a point."""
        data["cycles"] = int(data.get("cycles", 0)) + 1
        kept = []
        for c in data["candidates"]:
            if c.get("id") not in touched:
                c["importance"] = int(c.get("importance", 0)) - 1
            if c["importance"] > 0:
                kept.append(c)
            else:
                data["stats"]["forgotten"] = data["stats"].get("forgotten", 0) + 1
        data["candidates"] = kept

    def _evict(self, data: dict) -> None:
        if len(data["candidates"]) > MAX_CANDIDATES:
            data["candidates"].sort(key=lambda c: (int(c.get("importance", 0)), c.get("reinforced", 0)), reverse=True)
            dropped = len(data["candidates"]) - MAX_CANDIDATES
            data["candidates"] = data["candidates"][:MAX_CANDIDATES]
            data["stats"]["forgotten"] = data["stats"].get("forgotten", 0) + dropped

    def forget(self, needle: str) -> int:
        with self.lock():
            data = self.load()
            before = len(data["candidates"])
            data["candidates"] = [c for c in data["candidates"] if needle.casefold() not in c["text"].casefold()]
            removed = before - len(data["candidates"])
            if needle.casefold() in data.get("summary", "").casefold():
                sentences = re.split(r"(?<=[.!?])\s+", data["summary"])
                data["summary"] = " ".join(s for s in sentences if needle.casefold() not in s.casefold())
                removed += 1
            self.save(data)
        return removed

    def summary_due(self, data: dict, durable: bool) -> bool:
        if durable:
            return True
        words = len(str(data.get("summary", "")).split())
        return words > SUMMARY_WORDS or int(data.get("cycles", 0)) - int(data.get("summary_cycle", 0)) >= REWRITE_EVERY

    def clear(self) -> None:
        with self.lock():
            self.save(_empty())
        self.clear_recent()


def _age(ts: float) -> str:
    delta = max(0, time.time() - float(ts or 0))
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"

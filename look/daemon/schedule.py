"""One scheduler for everyone.

3.x had LOOK's `lk schedule` and Future Crash's separate "Threads" store. Here
both are entries in profile/schedule.json with an owner. A due entry becomes
an ordinary queued job, so it gets the same lane priority, the same policy
(non-interactive: anything needing a person is denied) and the same journal.

A thread can never broaden its own authority: its access is fixed when the
person creates it and capped at the creator's current profile.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import List, Optional

from .. import store
from ..paths import roots

MIN_INTERVAL = 60
MAX_ENTRIES = 24


def parse_every(text: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+)\s*(s|sec|m|min|h|hr|d|day)s?\s*", str(text or ""), re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()[0]
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def next_daily(hhmm: str, now: Optional[float] = None) -> Optional[float]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(hhmm or ""))
    if not m:
        return None
    now = now or time.time()
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
    return target if target > now else target + 86400


def load() -> List[dict]:
    data = store.read_json(roots.schedule, {}) or {}
    return [e for e in data.get("entries", []) if isinstance(e, dict)]


def save(entries: List[dict]) -> None:
    store.write_json(roots.schedule, {"schema": 1, "entries": entries})


def add(prompt: str, every: Optional[int] = None, daily: Optional[str] = None, delay: Optional[int] = None,
        owner: str = "lo", access: str = "conservative", workspace: str = "", title: str = "") -> dict:
    entries = load()
    if len([e for e in entries if e.get("enabled", True)]) >= MAX_ENTRIES:
        raise ValueError(f"at most {MAX_ENTRIES} active scheduled entries")
    if every is not None and every < MIN_INTERVAL:
        raise ValueError(f"minimum interval is {MIN_INTERVAL}s")
    now = time.time()
    if daily:
        first = next_daily(daily, now)
        if first is None:
            raise ValueError("daily time must look like 09:30")
    elif every:
        first = now + every
    else:
        first = now + max(0, delay or 0)
    entry = {"id": uuid.uuid4().hex[:6], "title": title or prompt[:40], "prompt": prompt, "owner": owner,
             "access": access, "workspace": workspace, "every": every, "daily": daily, "next": first,
             "enabled": True, "runs": 0, "last_run": None, "created": now}
    entries.append(entry)
    save(entries)
    return entry


def update(entry_id: str, **changes) -> Optional[dict]:
    entries = load()
    for e in entries:
        if e["id"] == entry_id:
            e.update({k: v for k, v in changes.items() if k in {"enabled", "prompt", "title", "every", "daily"}})
            save(entries)
            return e
    return None


def remove(entry_id: str) -> bool:
    entries = load()
    kept = [e for e in entries if e["id"] != entry_id]
    save(kept)
    return len(kept) != len(entries)


def dispatch_due(now: Optional[float] = None) -> int:
    """Turn due entries into queued jobs. Missed runs collapse into one."""
    from . import jobs
    now = now or time.time()
    entries = load()
    fired = 0
    for e in entries:
        if not e.get("enabled", True) or float(e.get("next") or 0) > now:
            continue
        jobs.enqueue("job", {"prompt": e["prompt"], "access": e.get("access", "conservative"),
                             "workspace": e.get("workspace") or str(roots.home), "owner": e.get("owner", "lo"),
                             "persona": e.get("owner", "lo"), "title": e.get("title", ""), "schedule_id": e["id"]},
                     spawn=False)
        fired += 1
        e["runs"] = int(e.get("runs", 0)) + 1
        e["last_run"] = now
        if e.get("daily"):
            e["next"] = next_daily(e["daily"], now + 60)
        elif e.get("every"):
            nxt = float(e["next"])
            while nxt <= now:
                nxt += int(e["every"])
            e["next"] = nxt
        else:
            e["enabled"] = False
    if fired:
        save(entries)
    return fired


def describe(e: dict) -> str:
    if e.get("daily"):
        when = f"daily {e['daily']}"
    elif e.get("every"):
        s = int(e["every"])
        when = f"every {s // 86400}d" if s % 86400 == 0 else f"every {s // 3600}h" if s % 3600 == 0 else f"every {s // 60}m"
    else:
        when = "once"
    return when

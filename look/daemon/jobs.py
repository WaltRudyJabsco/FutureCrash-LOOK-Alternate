"""Durable background work.

A job is a JSON file in runtime/queue/<kind>/. The files are the queue: a
crash, a reboot or a missing daemon loses nothing. Whoever drains — the
resident `looked` or a short-lived detached drainer — runs the same code.

Kinds, in priority order:
    job       explicit background LO work (`lo bg ...`, scheduled prompts)
    reflect   memory + skill reflection on a finished exchange
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .. import store
from ..log import log
from ..paths import roots

KINDS = ("job", "reflect")
MAX_ATTEMPTS = 5


def _dir(kind: str) -> Path:
    return roots.queue / kind


def enqueue(kind: str, payload: dict, spawn: bool = True) -> str:
    if kind not in KINDS:
        raise ValueError(kind)
    jid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    store.write_json(_dir(kind) / f"{jid}.json", {"id": jid, "kind": kind, "created": time.time(),
                                                   "attempts": 0, "payload": payload})
    if spawn:
        wake_or_spawn()
    return jid


def pending(kind: Optional[str] = None) -> List[Tuple[str, Path]]:
    out = []
    for k in ([kind] if kind else KINDS):
        try:
            out += [(k, p) for p in sorted(_dir(k).glob("*.json"))]
        except FileNotFoundError:
            continue
    return out


def counts() -> Dict[str, int]:
    return {k: len(pending(k)) for k in KINDS}


def wake_or_spawn() -> None:
    from .client import send
    if send({"op": "wake"}, timeout=0.3) is not None:
        return
    if os.environ.get("LOOK_NO_SPAWN"):
        return
    lockfile = roots.runtime / "drain.lock"
    probe = store.try_lock(lockfile)
    if probe is None:
        return  # a drainer is already running and will see the new file
    probe.close()
    try:
        subprocess.Popen([sys.executable, "-m", "look", "_drain"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")]))))
    except OSError as exc:
        log.warn("jobs.spawn", str(exc))


def run_one(kind: str, path: Path) -> str:
    """Execute a single job file. Returns done | retry | dropped."""
    from ..kernel.journal import journal
    record = store.read_json(path, None)
    if not isinstance(record, dict):
        path.unlink(missing_ok=True)
        return "dropped"
    handler = HANDLERS.get(kind)
    try:
        report = handler(record["payload"]) if handler else {"error": "no handler"}
        path.unlink()
        journal.event(f"{kind}.done", id=record["id"], report=report)
        return "done"
    except Exception as exc:  # boundary: failure keeps the job for retry
        record["attempts"] = int(record.get("attempts", 0)) + 1
        record["last_error"] = str(exc)
        if record["attempts"] >= MAX_ATTEMPTS:
            failed = roots.queue / "failed"
            failed.mkdir(parents=True, exist_ok=True)
            store.write_json(failed / path.name, record)
            path.unlink()
            journal.event(f"{kind}.failed", id=record["id"], error=str(exc))
            log.warn(f"jobs.{kind}", f"gave up on {record['id']}: {exc}")
            return "dropped"
        store.write_json(path, record)
        log.warn(f"jobs.{kind}", f"retry later {record['id']}: {exc}")
        return "retry"


def _reflect(payload: dict) -> dict:
    from ..kernel.ollama import Ollama
    from ..mind import personas
    from ..mind.memory import Mind
    from ..mind.reflect import reflect
    from ..settings import settings
    persona = personas.get(payload.get("persona", "lo")) or personas.LO
    model = settings.get("ai.fast_model") or payload.get("model") or settings.get("ai.model")
    client = Ollama(payload.get("host"))
    return reflect(Mind(payload.get("mind", persona.mind_name)),
                   {"user": payload.get("user", ""), "assistant": payload.get("assistant", "")},
                   client, model, payload.get("access", "workspace"), learn_skills=persona.learns_skills)


def _job(payload: dict) -> dict:
    """Run an LO prompt unattended. Anything needing a person's answer is denied, not guessed."""
    from ..kernel.agent import Agent
    from ..kernel.ollama import Ollama
    from ..mind import personas
    persona = personas.get(payload.get("persona", "lo")) or personas.LO
    agent = Agent(persona, client=Ollama(payload.get("host")), model=payload.get("model"),
                  access=payload.get("access"), workspace=Path(payload.get("workspace") or Path.home()),
                  interactive=False, lane_client="job")
    turn = agent.turn(payload["prompt"])
    out = {"prompt": payload["prompt"][:200], "answer": turn.answer, "ledger": turn.ledger(),
           "receipts": [r.receipt for _, _, r in turn.results], "owner": payload.get("owner", "lo"),
           "title": payload.get("title", "")}
    if turn.error:
        raise RuntimeError(turn.error)
    return out


_backoff_until = 0.0
HANDLERS: Dict[str, Callable[[dict], dict]] = {"reflect": _reflect, "job": _job}


def drain(stop: Optional[Callable[[], bool]] = None, wait_idle: bool = True) -> int:
    """Process the queue until empty. Background work always yields to people."""
    global _backoff_until
    from ..kernel import lane
    done = 0
    while True:
        if stop and stop():
            return done
        items = pending()
        if not items or time.time() < _backoff_until:
            return done
        if wait_idle and not lane.idle():
            time.sleep(1.0)
            continue
        kind, path = items[0]
        outcome = run_one(kind, path)
        if outcome == "done":
            done += 1
        elif outcome == "retry":
            _backoff_until = time.time() + 30.0  # e.g. Ollama unreachable: try again later
            return done

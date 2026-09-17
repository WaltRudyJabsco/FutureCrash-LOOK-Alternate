"""Reflection: one structured pass per exchange.

3.x ran separate model calls for candidate extraction, semantic matching,
skill extraction and feedback reflection, each with its own `NN|text` or
`SKILL|...` line format and its own cleanup regexes. 4.0 asks once, with an
Ollama JSON schema, and validates the object:

    {"memories": [{"text": "...", "importance": 0-100, "reinforces": "id or empty"}],
     "skill": "a reusable technique, or empty",
     "durable": true when the user explicitly asked for something to be kept long-term}

A summary rewrite is a second call that only happens when due.
Reflection runs in the background lane (daemon or detached drain), never while
someone is waiting on an answer.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

from ..log import log
from ..settings import settings
from . import skills
from .memory import SUMMARY_WORDS, Mind

SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "importance": {"type": "integer"}, "reinforces": {"type": "string"}},
            "required": ["text", "importance"]}},
        "skill": {"type": "string"},
        "durable": {"type": "boolean"},
    },
    "required": ["memories", "skill", "durable"],
}

INSTRUCTIONS = """You maintain the long-term memory of a terminal assistant. Read one exchange and decide what is worth remembering about the USER and their ongoing work.

Capture: stated preferences, favourites, habits, projects and decisions, unresolved tasks, anything they ask to be remembered.
Skip: small talk, facts about the world, the assistant's own output, transient details (a one-off path, a temporary port).
Importance: 90+ explicit "remember this"; 60-80 durable preferences/decisions; 30-50 useful context; below 30 do not bother.
If a new memory restates an existing one, set "reinforces" to that id instead of duplicating.

"skill": only if the exchange taught ONE reusable technique for doing assistant work better on many future tasks (no user facts, file names, model names). Otherwise "".
"durable": true only when the user explicitly wants something kept long-term.

Respond with the JSON object only."""

EXPLICIT = re.compile(r"\b(remember (this|that)|don'?t forget|keep in mind|note that|for future reference|"
                      r"i (always|never) |i prefer|my favou?rite)\b", re.I)
META = re.compile(r"\b(the assistant|as an ai|the exchange|this conversation|json|memory system)\b", re.I)


def strip_reasoning(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    return text.strip()


def parse(raw: str) -> Optional[Dict[str, Any]]:
    raw = strip_reasoning(raw)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    mems = []
    for item in obj.get("memories") or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text", "")).split())
        if not text or META.search(text):
            continue
        try:
            importance = int(item.get("importance", 50))
        except (TypeError, ValueError):
            importance = 50
        if importance < 30:
            continue
        mems.append({"text": text, "importance": importance, "reinforces": str(item.get("reinforces") or "") or None})
    return {"memories": mems[:5], "skill": str(obj.get("skill") or "").strip(), "durable": bool(obj.get("durable"))}


def reflect(mind: Mind, exchange: Dict[str, str], client, model: str, access: str = "workspace",
            learn_skills: bool = True) -> Dict[str, Any]:
    """Apply one exchange to a mind. Returns a small report for the journal."""
    user = exchange.get("user", "")
    assistant = exchange.get("assistant", "")
    report: Dict[str, Any] = {"mind": mind.name, "added": 0, "reinforced": 0, "skill": None, "summary": False}

    with mind.lock():
        data = mind.load()
    existing = "\n".join(f"{c['id']}: {c['text']}" for c in data["candidates"]) or "(none)"
    prompt = f"EXISTING MEMORIES:\n{existing}\n\nEXCHANGE\nuser: {user[:3000]}\nassistant: {assistant[:2000]}"
    parsed = None
    try:
        raw = client.chat(model, [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": prompt}],
                          format=SCHEMA, think=False, options={"temperature": 0.1, "num_ctx": 8192})
        parsed = parse(raw)
    except Exception as exc:  # boundary: the queued exchange stays queued for retry
        log.warn("reflect", f"model call failed: {exc}")
        raise

    if parsed is None:
        parsed = {"memories": [], "skill": "", "durable": False}
        report["invalid"] = True
    # Deterministic floor: explicit memory language never silently vanishes.
    if EXPLICIT.search(user) and not parsed["memories"]:
        sentence = next((s for s in re.split(r"(?<=[.!?])\s+", user) if EXPLICIT.search(s)), user)
        parsed["memories"].append({"text": sentence.strip()[:300], "importance": 85, "reinforces": None})
        report["fallback"] = True

    with mind.lock():
        data = mind.load()
        touched = set()
        for m in parsed["memories"]:
            before = {c["id"] for c in data["candidates"]}
            outcome = mind.add(data, m["text"], m["importance"], m["reinforces"])
            if outcome == "added":
                report["added"] += 1
                touched |= {c["id"] for c in data["candidates"]} - before
            elif outcome == "reinforced":
                report["reinforced"] += 1
                touched.add(m["reinforces"] or _closest_id(data, m["text"]))
        mind.decay(data, touched)
        data["last_evidence_at"] = time.time()
        data["stats"]["reflections"] = data["stats"].get("reflections", 0) + 1
        mind.save(data)
        summary_due = mind.summary_due(data, parsed["durable"])

    if summary_due:
        report["summary"] = rewrite_summary(mind, client, model)

    if learn_skills and parsed["skill"] and access != "conservative" and settings.get("skills.learning"):
        report["skill"] = skills.learn(parsed["skill"])
    return report


def _closest_id(data: dict, text: str) -> str:
    from .memory import similar
    best = max(data["candidates"], key=lambda c: similar(c["text"], text), default=None)
    return best["id"] if best else ""


SUMMARY_INSTRUCTIONS = f"""Rewrite a long-term memory summary about a user. Input: the current summary and the strongest current memories.
Keep what is durable, remove what is stale, contradicted, superseded, redundant or trivial. Plain prose, third person ("The user ..."), under {SUMMARY_WORDS} words.
Output only the summary text."""


def rewrite_summary(mind: Mind, client, model: str) -> bool:
    with mind.lock():
        data = mind.load()
    strong = sorted(data["candidates"], key=lambda c: int(c.get("importance", 0)), reverse=True)[:12]
    prompt = f"CURRENT SUMMARY:\n{data.get('summary') or '(empty)'}\n\nSTRONG MEMORIES:\n" + \
             "\n".join(f"- [{c['importance']}] {c['text']}" for c in strong)
    try:
        text = strip_reasoning(client.chat(model, [{"role": "system", "content": SUMMARY_INSTRUCTIONS},
                                                   {"role": "user", "content": prompt}],
                                           think=False, options={"temperature": 0.2, "num_ctx": 8192}))
    except Exception as exc:
        log.warn("reflect.summary", str(exc))
        return False
    text = " ".join(text.split())
    words = len(text.split())
    if not text or words > SUMMARY_WORDS * 1.15 or META.search(text) or text.startswith("{"):
        log.info("reflect.summary", f"rejected rewrite ({words} words); keeping previous summary")
        return False
    with mind.lock():
        data = mind.load()
        data["summary"] = text
        data["summary_cycle"] = data.get("cycles", 0)
        mind.save(data)
    return True

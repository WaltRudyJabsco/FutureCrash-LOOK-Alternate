"""Skills: LO's accumulated craft, kept in one editable Markdown file.

Memory is about the user. Skills are about how to do the job. The file has a
reviewed `Bundled` section (replaced when the program ships a newer pack) and a
`Learned` section (never touched by upgrades, capped at 24, deduplicated).
"""
from __future__ import annotations

import re
from typing import List, Tuple

from .. import store
from ..paths import BUNDLED, roots
from .memory import similar

MAX_LEARNED = 24


def _parse(text: str) -> Tuple[int, List[str], List[str]]:
    version = 0
    m = re.search(r"bundled-version:\s*(\d+)", text)
    if m:
        version = int(m.group(1))
    section = None
    bundled: List[str] = []
    learned: List[str] = []
    for line in text.splitlines():
        head = line.strip().lower()
        if head.startswith("## "):
            section = "bundled" if "bundled" in head else "learned" if "learned" in head else None
            continue
        if line.strip().startswith("- ") and section:
            (bundled if section == "bundled" else learned).append(line.strip()[2:].strip())
    return version, bundled, learned


def _render(version: int, bundled: List[str], learned: List[str]) -> str:
    out = ["# LO Skills", "", "<!-- schema: 1 -->", f"<!-- bundled-version: {version} -->", "",
           "Reusable craft. Edit freely: Bundled is refreshed by upgrades, Learned is yours.", "", "## Bundled", ""]
    out += [f"- {s}" for s in bundled]
    out += ["", "## Learned", ""]
    out += [f"- {s}" for s in learned]
    return "\n".join(out) + "\n"


def bundled_pack() -> Tuple[int, List[str]]:
    version, bundled, _ = _parse(store.read_text(BUNDLED / "skills.md"))
    return version, bundled


def ensure() -> None:
    """Install or refresh Bundled craft while preserving Learned craft."""
    pack_version, pack = bundled_pack()
    current = store.read_text(roots.skills)
    if not current.strip():
        store.write_text(roots.skills, _render(pack_version, pack, []))
        return
    version, _, learned = _parse(current)
    if pack_version > version:
        store.write_text(roots.skills, _render(pack_version, pack, learned))


def load() -> Tuple[List[str], List[str]]:
    ensure()
    _, bundled, learned = _parse(store.read_text(roots.skills))
    return bundled, learned


def prompt_text() -> str:
    bundled, learned = load()
    rows = bundled + learned
    return "CRAFT (how you work):\n" + "\n".join(f"- {s}" for s in rows) if rows else ""


TOO_SPECIFIC = re.compile(r"(/|~|\\|\.[a-z0-9]{1,5}\b|\b[a-z0-9-]+:[0-9a-z.]+b?\b|\bhttps?:)", re.I)


def acceptable(text: str) -> bool:
    text = " ".join(str(text or "").split())
    if not (12 <= len(text) <= 200):
        return False
    if TOO_SPECIFIC.search(text):
        return False  # filenames, paths, model tags, URLs are facts, not craft
    if re.search(r"\b(the user|sasha|i told|you said|this conversation)\b", text, re.I):
        return False
    return True


def learn(text: str) -> str:
    """Returns 'learned' | 'duplicate' | 'rejected'."""
    text = " ".join(str(text or "").split()).rstrip(".") + "."
    if not acceptable(text):
        return "rejected"
    with store.locked(roots.skills.with_suffix(".lock")):
        version, bundled, learned = _parse(store.read_text(roots.skills))
        if any(similar(text, s) >= 0.55 for s in bundled + learned):
            return "duplicate"
        learned = (learned + [text])[-MAX_LEARNED:]
        store.write_text(roots.skills, _render(version, bundled, learned))
    return "learned"


def add(text: str) -> str:
    text = " ".join(text.split())
    with store.locked(roots.skills.with_suffix(".lock")):
        version, bundled, learned = _parse(store.read_text(roots.skills))
        learned = (learned + [text])[-MAX_LEARNED:]
        store.write_text(roots.skills, _render(version, bundled, learned))
    return "learned"


def forget(needle: str) -> int:
    with store.locked(roots.skills.with_suffix(".lock")):
        version, bundled, learned = _parse(store.read_text(roots.skills))
        kept = [s for s in learned if needle.casefold() not in s.casefold()]
        store.write_text(roots.skills, _render(version, bundled, kept))
    return len(learned) - len(kept)


def clear_learned() -> int:
    with store.locked(roots.skills.with_suffix(".lock")):
        version, bundled, learned = _parse(store.read_text(roots.skills))
        store.write_text(roots.skills, _render(version, bundled, []))
    return len(learned)

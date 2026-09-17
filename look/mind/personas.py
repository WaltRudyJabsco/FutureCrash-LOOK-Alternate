"""Personas: who is speaking, and which mind they own.

A persona is voice + private memory. It is never permission: the access
profile is chosen separately and the same tools are offered to every persona
that may use them.

    lo            LO, with a selectable personality pack, core.md and skills.md
    future-crash  the Oracle, a fixed temperament and its own memory
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .. import store
from ..paths import BUNDLED, roots
from ..settings import settings
from . import skills
from .memory import Mind


def ensure_profile() -> None:
    roots.ensure()
    if not roots.core.exists():
        store.write_text(roots.core, store.read_text(BUNDLED / "core.md"))
    roots.personalities.mkdir(parents=True, exist_ok=True)
    for pack in (BUNDLED / "personalities").glob("*.md"):
        target = roots.personalities / pack.name
        if not target.exists():
            store.write_text(target, store.read_text(pack))
    skills.ensure()


def personalities() -> List[str]:
    names = {p.stem for p in (BUNDLED / "personalities").glob("*.md")}
    names |= {p.stem for p in roots.personalities.glob("*.md")}
    return sorted(names)


def personality_text(name: str) -> str:
    for base in (roots.personalities, BUNDLED / "personalities"):
        p = base / f"{name}.md"
        if p.exists():
            return store.read_text(p).strip()
    return store.read_text(BUNDLED / "personalities" / "lo.md").strip()


@dataclass
class Persona:
    name: str          # tool/persona key
    title: str         # shown in the terminal
    mind_name: str
    learns_skills: bool

    @property
    def mind(self) -> Mind:
        return Mind(self.mind_name)

    def identity(self) -> str:
        if self.name == "future-crash":
            custom = roots.profile / "future-crash.md"
            return store.read_text(custom).strip() or store.read_text(BUNDLED / "future-crash.md").strip()
        core = store.read_text(roots.core).strip() or store.read_text(BUNDLED / "core.md").strip()
        return core + "\n\n" + personality_text(settings.get("lo.personality"))

    def craft(self) -> str:
        return skills.prompt_text() if self.learns_skills else ""


LO = Persona("lo", "LO", "lo", learns_skills=True)
FUTURE_CRASH = Persona("future-crash", "FUTURE CRASH", "future-crash", learns_skills=False)


def get(name: str) -> Optional[Persona]:
    return {"lo": LO, "future-crash": FUTURE_CRASH}.get(name)

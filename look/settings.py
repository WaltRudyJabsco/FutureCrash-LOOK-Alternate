"""Declarative settings.

3.x kept one tiny file per preference (lo_access, lo_thinking, ...). 4.0 keeps
one `settings.json` in the profile, validated against the table below. The
table is also what `lk settings` renders, what `lk set` accepts, and what
completion offers — one definition, three uses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import store
from .paths import roots


@dataclass(frozen=True)
class Setting:
    key: str
    default: Any
    help: str
    choices: Optional[Sequence[Any]] = None
    kind: type = str
    env: Optional[str] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    def coerce(self, raw: Any) -> Any:
        if self.kind is bool:
            if isinstance(raw, bool):
                value = raw
            else:
                text = str(raw).strip().lower()
                if text in {"1", "on", "true", "yes", "y"}:
                    value = True
                elif text in {"0", "off", "false", "no", "n"}:
                    value = False
                else:
                    raise ValueError(f"{self.key}: expected on/off")
        elif self.kind is int:
            value = int(raw)
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.key}: minimum is {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.key}: maximum is {self.maximum}")
        else:
            value = str(raw).strip()
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"{self.key}: choose one of {', '.join(map(str, self.choices))}")
        return value


ACCESS = ("conservative", "workspace", "power", "unsafe")

TABLE: List[Setting] = [
    Setting("ai.model", "qwen3:8b", "Model shared by LO and Future Crash", env="LOOK_MODEL"),
    Setting("ai.fast_model", "", "Optional small model for background reflection"),
    Setting("lo.access", "workspace", "What LO may do", choices=ACCESS, env="LOOK_ACCESS"),
    Setting("lo.personality", "lo", "LO's voice (never its permissions)"),
    Setting("lo.thinking", "adaptive", "Reasoning depth", choices=("light", "adaptive", "deep")),
    Setting("lo.think_display", "compact", "How live thinking is shown", choices=("compact", "full", "quiet")),
    Setting("lo.max_rounds", 16, "Ceiling on tool rounds per turn", kind=int, minimum=1, maximum=64),
    Setting("lo.ledger", "always", "Turn ledger: every turn, or only turns with actions", choices=("always", "actions")),
    Setting("memory.enabled", True, "Remember across sessions", kind=bool),
    Setting("skills.learning", True, "Let LO learn reusable craft (never in conservative)", kind=bool),
    Setting("feedback.sound", False, "Semantic sound cues", kind=bool),
    Setting("feedback.motion", "subtle", "Terminal motion", choices=("off", "subtle", "normal")),
    Setting("crash.authority", "conservative", "What the Future Crash Oracle may do", choices=ACCESS),
    Setting("crash.ambient", True, "Oracle speaks up on its own when the lane is idle", kind=bool),
    Setting("crash.fps", 12, "Future Crash frame rate", kind=int, minimum=2, maximum=30),
    Setting("shell.short_aliases", True, "Install l/ll/lr/... shortcuts in shell-init", kind=bool),
]

BY_KEY: Dict[str, Setting] = {s.key: s for s in TABLE}


class Settings:
    def __init__(self):
        self._cache: Optional[Dict[str, Any]] = None
        self._mtime = None

    def _load(self) -> Dict[str, Any]:
        path = roots.settings
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is None or mtime != self._mtime:
            data = store.read_json(path, {}) or {}
            self._cache = data.get("values", {}) if isinstance(data, dict) else {}
            self._mtime = mtime
        return self._cache

    def get(self, key: str) -> Any:
        spec = BY_KEY[key]
        if spec.env and os.environ.get(spec.env):
            try:
                return spec.coerce(os.environ[spec.env])
            except ValueError:
                pass
        raw = self._load().get(key, spec.default)
        try:
            return spec.coerce(raw)
        except (ValueError, TypeError):
            return spec.default

    def set(self, key: str, value: Any) -> Any:
        if key not in BY_KEY:
            raise KeyError(f"unknown setting: {key}")
        clean = BY_KEY[key].coerce(value)
        values = dict(self._load())
        values[key] = clean
        store.write_json(roots.settings, {"schema": 1, "values": values})
        self._cache = None
        return clean

    def reset(self, key: str) -> None:
        values = dict(self._load())
        values.pop(key, None)
        store.write_json(roots.settings, {"schema": 1, "values": values})
        self._cache = None

    def explicit(self) -> Dict[str, Any]:
        return dict(self._load())


settings = Settings()


def secret(name: str) -> str:
    """Environment first, then the machine-local 0600 secrets file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    data = store.read_json(roots.secrets, {}) or {}
    return str(data.get(name, "")).strip()


def set_secret(name: str, value: str) -> None:
    data = store.read_json(roots.secrets, {}) or {}
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    store.write_json(roots.secrets, data, private=True)

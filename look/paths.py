"""The four kinds of state, named once.

    PROGRAM   replaceable distribution material (this package, bundled packs)
    PROFILE   the user's durable, portable LOOK (memory, skills, core, settings)
    MACHINE   true on one machine only (hosts, grants, secrets, app choices)
    RUNTIME   disposable (sockets, queues, journal archives, caches)

Program may be reinstalled. Runtime may be discarded. Machine state may be
reconfigured. Profile is the user's durable LOOK.

Everything that touches disk asks this module where things live. Setting
LOOK_HOME relocates all four roots under one directory (used by tests and by
people who want a self-contained install).
"""
from __future__ import annotations

import os
from pathlib import Path

PROGRAM = Path(__file__).resolve().parent
BUNDLED = PROGRAM / "bundled"


class Roots:
    """Resolved lazily so tests can repoint LOOK_HOME before first use."""

    @property
    def home(self) -> Path:
        return Path(os.environ.get("HOME") or Path.home())

    def _xdg(self, env: str, default: str) -> Path:
        raw = os.environ.get(env)
        return (Path(raw).expanduser() if raw else self.home / default) / "look"

    def _override(self, leaf: str):
        raw = os.environ.get("LOOK_HOME")
        return Path(raw).expanduser() / leaf if raw else None

    @property
    def profile(self) -> Path:
        return self._override("profile") or self._xdg("XDG_DATA_HOME", ".local/share") / "profile"

    @property
    def machine(self) -> Path:
        return self._override("machine") or self._xdg("XDG_CONFIG_HOME", ".config")

    @property
    def runtime(self) -> Path:
        return self._override("runtime") or self._xdg("XDG_STATE_HOME", ".local/state")

    # ---- profile ---------------------------------------------------------
    @property
    def settings(self) -> Path: return self.profile / "settings.json"
    @property
    def core(self) -> Path: return self.profile / "core.md"
    @property
    def skills(self) -> Path: return self.profile / "skills.md"
    @property
    def personalities(self) -> Path: return self.profile / "personalities"
    @property
    def minds(self) -> Path: return self.profile / "minds"

    def mind(self, name: str) -> Path:
        """Each persona owns a private mind: memory + recent continuity."""
        return self.minds / name

    @property
    def schedule(self) -> Path: return self.profile / "schedule.json"

    # ---- machine ---------------------------------------------------------
    @property
    def hosts(self) -> Path: return self.machine / "hosts.json"
    @property
    def grants(self) -> Path: return self.machine / "grants.json"
    @property
    def apps(self) -> Path: return self.machine / "apps.json"
    @property
    def secrets(self) -> Path: return self.machine / "secrets.json"
    @property
    def backups(self) -> Path: return self.machine / "backups.json"

    # ---- runtime ---------------------------------------------------------
    @property
    def journal(self) -> Path: return self.runtime / "journal.jsonl"
    @property
    def trash(self) -> Path: return self.runtime / "trash"
    @property
    def queue(self) -> Path: return self.runtime / "queue"
    @property
    def socket(self) -> Path: return self.runtime / "looked.sock"
    @property
    def pidfile(self) -> Path: return self.runtime / "looked.pid"
    @property
    def log(self) -> Path: return self.runtime / "look.log"
    @property
    def leases(self) -> Path: return self.runtime / "leases"
    @property
    def cache(self) -> Path: return self.runtime / "cache"
    @property
    def handoff(self) -> Path: return self.runtime / "handoff"
    @property
    def canvas(self) -> Path: return self.runtime / "signal.json"

    def ensure(self) -> None:
        for p in (self.profile, self.minds, self.personalities, self.machine,
                  self.runtime, self.trash, self.queue, self.leases, self.cache, self.handoff):
            p.mkdir(parents=True, exist_ok=True)
        for p in (self.profile, self.machine, self.runtime):
            try:
                p.chmod(0o700)
            except OSError:
                pass

    # ---- legacy (3.x) ----------------------------------------------------
    @property
    def legacy_state(self) -> Path:
        return self.home / ".local" / "share" / "look"

    @property
    def legacy_future_crash(self) -> Path:
        return self.home / ".future_crash"


roots = Roots()

"""Intelligence is separate from permission.

The whole permission model is this table plus the path boundary below. It is
model-independent: a tool the profile does not allow is never offered to the
model, and is refused again at invocation if a model names it anyway.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Set

from .. import store
from ..paths import roots
from .results import Result
from .tools import DRAW, EXEC, NET, OPEN, READ, TASKS, WRITE, Context

PROFILES: Dict[str, Set[str]] = {
    "conservative": {READ, NET, DRAW},
    "workspace":    {READ, NET, DRAW, WRITE, OPEN, TASKS},
    "power":        {READ, NET, DRAW, WRITE, OPEN, TASKS, EXEC},
    "unsafe":       {READ, NET, DRAW, WRITE, OPEN, TASKS, EXEC},
}

DESCRIPTIONS = {
    "conservative": "read, search and reason — no changes",
    "workspace": "edit inside the workspace; ask before reaching outside it",
    "power": "workspace + shell commands, each one confirmed",
    "unsafe": "unconfirmed shell commands with your full user privileges",
}

# Never readable or writable by an assistant, regardless of profile or grant.
SECRET_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", "Keychains", ".netrc"}
SYSTEM_ROOTS = ("/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/Library", "/boot", "/dev", "/proc", "/sys")


def capabilities(access: str) -> Set[str]:
    return set(PROFILES.get(access, PROFILES["conservative"]))


def allowed(access: str, capability: str) -> bool:
    return capability in capabilities(access)


def exec_needs_confirmation(access: str) -> bool:
    return access != "unsafe"


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve(ctx: Context, raw: str) -> Path:
    """Interpret a tool path: ~ expands, relative is relative to the workspace."""
    text = str(raw or "").strip()
    p = Path(os.path.expanduser(text))
    if not p.is_absolute():
        p = ctx.workspace / p
    # resolve() without strict so not-yet-existing targets still normalise.
    return Path(os.path.normpath(str(p.resolve() if p.exists() else p.parent.resolve() / p.name)))


def _is_secret(path: Path) -> bool:
    if any(part in SECRET_PARTS for part in path.parts):
        return True
    # LOOK's own machine and runtime state (grants, secrets, journal, trash)
    # are managed through lk commands, never by an assistant's file tools.
    for root in (roots.machine, roots.runtime):
        try:
            if within(path, root.resolve()):
                return True
        except OSError:
            continue
    return False


def _load_grants() -> list:
    data = store.read_json(roots.grants, {}) or {}
    return list(data.get("grants", []))


def _save_grant(path: Path, mode: str) -> None:
    grants = [g for g in _load_grants() if not (g.get("path") == str(path) and g.get("mode") == mode)]
    grants.append({"path": str(path), "mode": mode})
    store.write_json(roots.grants, {"schema": 1, "grants": grants})


def revoke_grant(path: str) -> int:
    grants = _load_grants()
    kept = [g for g in grants if g.get("path") != str(Path(path).expanduser())]
    store.write_json(roots.grants, {"schema": 1, "grants": kept})
    return len(grants) - len(kept)


def list_grants() -> list:
    return _load_grants()


def _granted(ctx: Context, path: Path, mode: str) -> bool:
    for scope_path, scope_mode in ctx.session_grants:
        if within(path, Path(scope_path)) and (scope_mode == mode or scope_mode == "write"):
            return True
    for g in _load_grants():
        if within(path, Path(g.get("path", "/nonexistent"))) and (g.get("mode") == mode or g.get("mode") == "write"):
            return True
    return False


def check_path(ctx: Context, path: Path, mode: str) -> Optional[Result]:
    """None means proceed. A Result means stop and return it.

    mode is "read" or "write".  Reads anywhere under $HOME are allowed (secret
    directories excepted); writes are free inside the workspace and anywhere
    else require a grant the user gives interactively.
    """
    if _is_secret(path):
        return Result.denied(f"{path} is a protected secret location")
    home = roots.home.resolve()
    in_workspace = within(path, ctx.workspace)

    if mode == "read":
        if in_workspace or within(path, home) or _granted(ctx, path, "read"):
            return None
        if ctx.access == "unsafe":
            return None
    else:
        if within(path, roots.profile.resolve()):
            return Result.denied("LOOK's profile is changed through `lk memory` / `lk skills`, not file tools")
        if in_workspace or _granted(ctx, path, "write"):
            return None
        if any(str(path).startswith(r + "/") or str(path) == r for r in SYSTEM_ROOTS) and ctx.access != "unsafe":
            return Result.denied(f"{path} is a system location; LO will not write there")

    scope = path if path.is_dir() else path.parent
    verb = "read" if mode == "read" else "change"
    answer = ctx.ask(
        f"{ctx.actor.upper()} wants to {verb} outside its workspace:\n  {path}",
        {"o": "once", "s": f"this session ({scope})", "a": f"always ({scope})", "n": "no"},
        default="n",
    )
    if answer == "o":
        return None
    if answer == "s":
        ctx.session_grants.add((str(scope), mode))
        return None
    if answer == "a":
        ctx.session_grants.add((str(scope), mode))
        _save_grant(scope, mode)
        return None
    return Result.denied(f"no permission to {verb} {path}", path=str(path), needs_grant=True)


def confirm_command(ctx: Context, command: str) -> bool:
    if not exec_needs_confirmation(ctx.access):
        return True
    for scope, mode in ctx.session_grants:
        if mode == "exec" and scope == command:
            return True
    answer = ctx.ask(f"Run this command?\n  $ {command}",
                     {"y": "yes, once", "s": "yes, and this exact command for the session", "n": "no"}, default="n")
    if answer == "s":
        ctx.session_grants.add((command, "exec"))
    return answer in {"y", "s"}

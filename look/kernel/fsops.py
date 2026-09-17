"""Reversible filesystem operations shared by LO's tools, the filer and lcp/lmv/lrm.

Every function returns a Result whose `undo` recipe the journal stores. None of
them overwrite anything: removal moves to runtime trash, overwrites keep a
backup, collisions refuse (moves) or pick a " copy" name (copies).
"""
from __future__ import annotations

import contextvars
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .journal import new_id, sha256, trash_slot
from .results import Result

MAX_WRITE_BYTES = 2_000_000
BASE: "contextvars.ContextVar[Optional[Path]]" = contextvars.ContextVar("look_display_base", default=None)


def display(path: Path, base: Optional[Path] = None) -> str:
    """Workspace-relative when inside the workspace, ~-relative under home, else absolute."""
    home = Path.home()
    base = base if base is not None else BASE.get()
    try:
        if base is not None:
            rel = path.relative_to(base)
            return str(rel) if str(rel) != "." else path.name
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _variant(target: Path) -> Path:
    stem, suffix = (target.stem, target.suffix) if target.is_file() or target.suffix else (target.name, "")
    n = 1
    while True:
        name = f"{stem} copy{'' if n == 1 else ' ' + str(n)}{suffix}"
        candidate = target.with_name(name)
        if not candidate.exists():
            return candidate
        n += 1


def _missing_dirs(path: Path) -> List[Path]:
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    return list(reversed(missing))


def _make_dirs(path: Path, items: List[dict]) -> None:
    for d in _missing_dirs(path):
        d.mkdir()
        items.append({"kind": "created_dir", "path": str(d)})


def plan_destination(sources: Sequence[Path], dest: Path, dest_raw: str = "") -> Dict[Path, Path]:
    """Decide final targets. A destination is a directory when it already is one,
    when it is spelled with a trailing slash, or when there are several sources."""
    as_dir = dest.is_dir() or dest_raw.endswith("/") or len(sources) > 1
    return {src: (dest / src.name if as_dir else dest) for src in sources}


def copy_paths(sources: Sequence[Path], dest: Path, dest_raw: str = "") -> Result:
    if not sources:
        return Result.refused("copy: nothing to copy")
    for src in sources:
        if not src.exists():
            return Result.refused(f"copy: {display(src)} does not exist")
    plan = plan_destination(sources, dest, dest_raw)
    items: List[dict] = []
    done: List[str] = []
    for src, target in plan.items():
        if target.resolve() == src.resolve() or target.exists():
            target = _variant(target)
        if src.is_dir() and _is_inside(target, src):
            return Result.refused(f"copy: cannot copy {display(src)} into itself")
        _make_dirs(target.parent, items)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, target, symlinks=True)
            items.append({"kind": "copied", "path": str(target)})
        else:
            shutil.copy2(src, target, follow_symlinks=False)
            items.append({"kind": "copied", "path": str(target), "sha": sha256(target)})
        done.append(display(target))
    receipt = f"copied {done[0]}" if len(done) == 1 else f"copied {_plural(len(done), 'item')} → {display(dest)}"
    return Result.done(receipt, changes=len(done), undo=_bundle(items), targets=done)


def move_paths(sources: Sequence[Path], dest: Path, dest_raw: str = "") -> Result:
    if not sources:
        return Result.refused("move: nothing to move")
    plan = plan_destination(sources, dest, dest_raw)
    for src, target in plan.items():
        if not src.exists() and not src.is_symlink():
            return Result.refused(f"move: {display(src)} does not exist")
        if target.exists() and target.resolve() != src.resolve():
            return Result.refused(f"move: {display(target)} already exists; nothing was moved")
        if src.is_dir() and _is_inside(target, src):
            return Result.refused(f"move: cannot move {display(src)} into itself")
    items: List[dict] = []
    done: List[str] = []
    for src, target in plan.items():
        if target == src:
            continue
        _make_dirs(target.parent, items)
        shutil.move(str(src), str(target))
        items.append({"kind": "moved", "from": str(target), "to": str(src)})
        done.append(f"{display(src)} → {display(target)}")
    if not done:
        return Result.refused("move: source and destination are the same")
    receipt = f"moved {done[0]}" if len(done) == 1 else f"moved {_plural(len(done), 'item')} → {display(dest)}"
    return Result.done(receipt, changes=len(done), undo=_bundle(items), moves=done)


def remove_paths(paths: Sequence[Path]) -> Result:
    if not paths:
        return Result.refused("remove: nothing to remove")
    for p in paths:
        if not p.exists() and not p.is_symlink():
            return Result.refused(f"remove: {display(p)} does not exist")
        if p == Path.home() or p == Path("/") or p.parent == p:
            return Result.refused(f"remove: refusing to remove {p}")
    change = new_id()
    items: List[dict] = []
    for p in paths:
        slot = trash_slot(change, p.name)
        shutil.move(str(p), str(slot))
        items.append({"kind": "removed", "from": str(slot), "to": str(p)})
    names = [display(p) for p in paths]
    receipt = f"removed {names[0]}" if len(names) == 1 else f"removed {_plural(len(names), 'item')}"
    return Result.done(receipt + " (kept in trash)", changes=len(names), undo=_bundle(items), removed=names)


def make_directory(path: Path) -> Result:
    if path.is_dir():
        return Result.done(f"{display(path)} already exists", existed=True)
    if path.exists():
        return Result.refused(f"mkdir: {display(path)} exists and is not a directory")
    items: List[dict] = []
    _make_dirs(path, items)
    return Result.done(f"created folder {display(path)}", changes=len(items), undo=_bundle(items))


def touch(path: Path, parents: bool = False) -> Result:
    if path.exists():
        return Result.refused(f"{display(path)} already exists")
    items: List[dict] = []
    if not path.parent.exists():
        if not parents:
            return Result.refused(f"{display(path.parent)} does not exist")
        _make_dirs(path.parent, items)
    path.touch(exist_ok=False)
    items.append({"kind": "created_file", "path": str(path), "sha": sha256(path)})
    return Result.done(f"created {display(path)}", changes=1, undo=_bundle(items))


def write_file(path: Path, content: str, mode: str = "create") -> Result:
    """mode: create (refuse if exists) | overwrite | append."""
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return Result.refused(f"write: {len(data)} bytes exceeds the {MAX_WRITE_BYTES} byte limit")
    if path.is_dir():
        return Result.refused(f"write: {display(path)} is a directory")
    existed = path.exists()
    if existed and mode == "create":
        return Result.refused(f"write: {display(path)} already exists; use mode overwrite or append")
    if existed and _looks_binary(path):
        return Result.refused(f"write: {display(path)} is not a text file")
    items: List[dict] = []
    _make_dirs(path.parent, items)
    backup = None
    if existed:
        backup = trash_slot(new_id(), path.name)
        shutil.copy2(path, backup)
    if mode == "append" and existed:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        tmp = path.with_name(f".{path.name}.look-tmp")
        tmp.write_text(content, encoding="utf-8")
        if existed:
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    after = sha256(path)
    if existed:
        items.append({"kind": "wrote_file", "path": str(path), "backup": str(backup), "sha_after": after})
        verb = "appended to" if mode == "append" else "rewrote"
    else:
        items.append({"kind": "created_file", "path": str(path), "sha": after})
        verb = "wrote"
    lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return Result.done(f"{verb} {display(path)} ({_plural(lines, 'line')})", changes=1, undo=_bundle(items),
                       path=str(path), bytes=len(data))


def create_files(files: Sequence[Dict[str, str]], resolve) -> Result:
    """Several new text files as one reversible change. `resolve` maps raw → Path."""
    targets = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            return Result.refused("create_files: every entry needs a path")
        p = resolve(f["path"])
        if p.exists():
            return Result.refused(f"create_files: {display(p)} already exists; nothing was created")
        targets.append((p, str(f.get("content", ""))))
    if len({p for p, _ in targets}) != len(targets):
        return Result.refused("create_files: duplicate paths in request")
    items: List[dict] = []
    for p, text in targets:
        _make_dirs(p.parent, items)
        p.write_text(text, encoding="utf-8")
        items.append({"kind": "created_file", "path": str(p), "sha": sha256(p)})
    names = [display(p) for p, _ in targets]
    return Result.done(f"created {_plural(len(names), 'file')}: {', '.join(names[:6])}{' …' if len(names) > 6 else ''}",
                       changes=len(names), undo=_bundle(items), paths=names)


def _bundle(items: List[dict]) -> Optional[dict]:
    if not items:
        return None
    return items[0] if len(items) == 1 else {"kind": "batch", "items": items}


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(4096)
    except OSError:
        return False

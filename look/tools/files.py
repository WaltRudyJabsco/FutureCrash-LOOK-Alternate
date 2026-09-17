"""Filesystem tools. Reads are bounded; changes are reversible."""
from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from ..kernel import fsops
from ..kernel.policy import check_path, resolve
from ..kernel.results import Result
from ..kernel.tools import CHANGE, READ, WRITE, Context, Param, registry

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".Trash", "Library", ".npm", ".cargo",
             ".rustup", "dist", "build", ".next", ".tox", ".mypy_cache", ".pytest_cache"}
READ_LIMIT = 60_000
SCAN_LIMIT = 40_000


def _human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _walk(root: Path, max_entries: int = SCAN_LIMIT, hidden: bool = False, deadline: float = 6.0):
    started = time.monotonic()
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and (hidden or not d.startswith(".")))
        for name in sorted(filenames):
            if not hidden and name.startswith("."):
                continue
            yield Path(dirpath) / name
            seen += 1
            if seen >= max_entries or time.monotonic() - started > deadline:
                return


@registry.tool("list_files", "List a directory (non-recursive): names, kinds, sizes, modified times.", READ,
               params={"path": Param("string", "Directory; default is the workspace", required=False),
                       "hidden": Param("boolean", "Include dotfiles", required=False)})
def list_files(ctx: Context, path: str = ".", hidden: bool = False) -> Result:
    target = resolve(ctx, path or ".")
    stop = check_path(ctx, target, "read")
    if stop:
        return stop
    if not target.is_dir():
        return Result.refused(f"{fsops.display(target)} is not a directory")
    rows = []
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
    for p in entries:
        if not hidden and p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        kind = "dir" if p.is_dir() else "file"
        rows.append(f"{kind:4}  {_human(st.st_size) if kind == 'file' else '-':>7}  "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}  {p.name}{'/' if kind == 'dir' else ''}")
    shown = rows[:400]
    return Result.done(f"listed {fsops.display(target)} ({len(rows)} entries)", "\n".join(shown) +
                       (f"\n… {len(rows) - 400} more" if len(rows) > 400 else ""), path=str(target), count=len(rows))


@registry.tool("inspect_directory", "Exact recursive counts and total size for a directory. Use for 'how many files/how big' questions.",
               READ, params={"path": Param("string", "Directory", required=False)})
def inspect_directory(ctx: Context, path: str = ".") -> Result:
    target = resolve(ctx, path or ".")
    stop = check_path(ctx, target, "read")
    if stop:
        return stop
    if not target.is_dir():
        return Result.refused(f"{fsops.display(target)} is not a directory")
    files = dirs = size = 0
    by_ext = {}
    truncated = False
    started = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(target):
        dirs += len(dirnames)
        for name in filenames:
            files += 1
            try:
                size += (Path(dirpath) / name).lstat().st_size
            except OSError:
                pass
            ext = Path(name).suffix.lower() or "(none)"
            by_ext[ext] = by_ext.get(ext, 0) + 1
        if time.monotonic() - started > 15:
            truncated = True
            break
    top = ", ".join(f"{e} {n}" for e, n in sorted(by_ext.items(), key=lambda kv: -kv[1])[:10])
    note = " (partial: time limit)" if truncated else ""
    return Result.done(f"{fsops.display(target)}: {files} files, {dirs} folders, {_human(size)}{note}",
                       f"by extension: {top}", files=files, folders=dirs, bytes=size, partial=truncated)


@registry.tool("read_file", "Read a UTF-8 text file (bounded). Use offset/lines for large files.", READ,
               params={"path": Param("string", "File to read"),
                       "offset": Param("integer", "First line (1-based)", required=False),
                       "lines": Param("integer", "How many lines", required=False)})
def read_file(ctx: Context, path: str, offset: int = 1, lines: Optional[int] = None) -> Result:
    target = resolve(ctx, path)
    stop = check_path(ctx, target, "read")
    if stop:
        return stop
    if not target.is_file():
        return Result.refused(f"{fsops.display(target)} is not a file")
    with open(target, "rb") as fh:
        head = fh.read(8192)
    if b"\x00" in head:
        return Result.refused(f"{fsops.display(target)} looks binary; not reading it as text")
    text = target.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    start = max(1, int(offset or 1))
    chunk = all_lines[start - 1:(start - 1 + int(lines)) if lines else None]
    body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, start))
    truncated = len(body) > READ_LIMIT
    if truncated:
        body = body[:READ_LIMIT]
    end = start + len(chunk) - 1
    return Result.done(f"read {fsops.display(target)} (lines {start}–{end} of {len(all_lines)})", body,
                       path=str(target), total_lines=len(all_lines), truncated=truncated)


@registry.tool("search_files", "Find files and folders by name under a directory (fuzzy words or glob).", READ,
               params={"query": Param("string", "Words or a glob like *.md"),
                       "path": Param("string", "Where to search; default workspace. Use ~ for home.", required=False)})
def search_files(ctx: Context, query: str, path: str = ".") -> Result:
    root = resolve(ctx, path or ".")
    stop = check_path(ctx, root, "read")
    if stop:
        return stop
    if not root.is_dir():
        return Result.refused(f"{fsops.display(root)} is not a directory")
    is_glob = any(c in query for c in "*?[")
    words = [w for w in re.split(r"\s+", query.casefold()) if w]
    scored = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in dirnames + filenames:
            if name.startswith("."):
                continue
            low = name.casefold()
            if is_glob:
                if not fnmatch.fnmatch(low, query.casefold()):
                    continue
                score = 1
            else:
                if not all(w in low for w in words):
                    continue
                score = 3 if low == query.casefold() else 2 if low.startswith(words[0]) else 1
            p = Path(dirpath) / name
            depth = len(p.relative_to(root).parts)
            scored.append((-score, depth, str(p)))
        if len(scored) > 2000:
            break
    scored.sort()
    rows = [fsops.display(Path(s[2])) + ("/" if Path(s[2]).is_dir() else "") for s in scored[:60]]
    if not rows:
        return Result.done(f"no names matching {query!r} under {fsops.display(root)}", matches=0)
    return Result.done(f"{len(scored)} match{'es' if len(scored) != 1 else ''} for {query!r} under {fsops.display(root)}",
                       "\n".join(rows), matches=len(scored))


@registry.tool("search_content", "Search inside text files for a phrase or regex; returns file:line matches.", READ,
               params={"pattern": Param("string", "Text or regular expression"),
                       "path": Param("string", "Directory or file; default workspace", required=False),
                       "glob": Param("string", "Only files matching, e.g. *.py", required=False)})
def search_content(ctx: Context, pattern: str, path: str = ".", glob: str = "") -> Result:
    root = resolve(ctx, path or ".")
    stop = check_path(ctx, root, "read")
    if stop:
        return stop
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    files = [root] if root.is_file() else list(_walk(root, max_entries=8000))
    hits: List[str] = []
    scanned = 0
    for f in files:
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            with open(f, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        scanned += 1
        for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{fsops.display(f, root if root.is_dir() else None)}:{n}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    break
        if len(hits) >= 200:
            break
    return Result.done(f"{len(hits)} line{'s' if len(hits) != 1 else ''} matching {pattern!r} in {scanned} files",
                       "\n".join(hits), matches=len(hits), scanned=scanned)


@registry.tool("write_file", "Create a new text file, or overwrite/append to an existing one (the old version is kept for undo).",
               WRITE, CHANGE, params={
                   "path": Param("string", "File path"),
                   "content": Param("string", "Full text to write"),
                   "mode": Param("string", "create (default, refuses if it exists), overwrite, or append", required=False,
                                 enum=["create", "overwrite", "append"])})
def write_file(ctx: Context, path: str, content: str, mode: str = "create") -> Result:
    target = resolve(ctx, path)
    stop = check_path(ctx, target, "write")
    return stop or fsops.write_file(target, str(content), mode or "create")


@registry.tool("create_files", "Create several new text files in one reversible operation.", WRITE, CHANGE,
               params={"files": Param("array", "Files to create", items={"type": "object", "properties": {
                   "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]})})
def create_files(ctx: Context, files: list) -> Result:
    paths = []
    for f in files or []:
        p = resolve(ctx, (f or {}).get("path", ""))
        stop = check_path(ctx, p, "write")
        if stop:
            return stop
        paths.append(p)
    return fsops.create_files(files, lambda raw: resolve(ctx, raw))


def _sources(ctx: Context, raw) -> List[Path]:
    items = raw if isinstance(raw, list) else [raw]
    return [resolve(ctx, str(s)) for s in items if str(s).strip()]


@registry.tool("copy_path", "Copy files or folders. Never overwrites: a clash gets a ' copy' name.", WRITE, CHANGE,
               params={"source": Param("string", "Path to copy (or several, comma-free, as a JSON list)"),
                       "destination": Param("string", "Target path or folder (end with / for a folder)")})
def copy_path(ctx: Context, source, destination: str) -> Result:
    sources = _sources(ctx, source)
    dest = resolve(ctx, destination)
    for s in sources:
        stop = check_path(ctx, s, "read")
        if stop:
            return stop
    stop = check_path(ctx, dest, "write")
    return stop or fsops.copy_paths(sources, dest, str(destination))


@registry.tool("move_path", "Move or rename files or folders. Refuses to overwrite.", WRITE, CHANGE,
               params={"source": Param("string", "Path to move (or a JSON list of paths)"),
                       "destination": Param("string", "New path or folder (end with / for a folder)")})
def move_path(ctx: Context, source, destination: str) -> Result:
    sources = _sources(ctx, source)
    dest = resolve(ctx, destination)
    for p in sources + [dest]:
        stop = check_path(ctx, p, "write")
        if stop:
            return stop
    return fsops.move_paths(sources, dest, str(destination))


@registry.tool("remove_path", "Remove files or folders (moved to LOOK's trash; `lk undo` restores).", WRITE, CHANGE,
               params={"path": Param("string", "Path to remove (or a JSON list of paths)")})
def remove_path(ctx: Context, path) -> Result:
    targets = _sources(ctx, path)
    for p in targets:
        stop = check_path(ctx, p, "write")
        if stop:
            return stop
        if p == ctx.workspace:
            return Result.refused("refusing to remove the workspace itself")
    return fsops.remove_paths(targets)


@registry.tool("make_directory", "Create a folder (and missing parents).", WRITE, CHANGE,
               params={"path": Param("string", "Folder path")})
def make_directory(ctx: Context, path: str) -> Result:
    target = resolve(ctx, path)
    stop = check_path(ctx, target, "write")
    return stop or fsops.make_directory(target)

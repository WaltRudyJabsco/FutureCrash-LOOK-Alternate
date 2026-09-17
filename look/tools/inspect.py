"""Read-only host inspection for LO. Available in every profile."""
from __future__ import annotations

from ..kernel.policy import check_path, resolve
from ..kernel.results import Result
from ..kernel.tools import READ, Context, Param, registry
from .. import system


@registry.tool("machine_info", "Machine summary: OS, CPU, memory, disk, load, uptime, GPU.", READ)
def machine_info(ctx: Context) -> Result:
    info = system.machine()
    g = system.gpu()
    if g:
        info["gpu"] = g
    body = "\n".join(f"{k}: {v}" for k, v in info.items())
    return Result.done(f"machine: {info['host']} · {info['memory']} memory", body, **info)


@registry.tool("list_ports", "Listening TCP ports and the processes that own them.", READ)
def list_ports(ctx: Context) -> Result:
    rows = system.ports()
    body = "\n".join(f"{r['port']:>6}  {r['process']:<20} pid {r['pid']:<7} {r['address']}" for r in rows)
    return Result.done(f"{len(rows)} listening port{'s' if len(rows) != 1 else ''}", body, count=len(rows))


@registry.tool("find_processes", "Running processes, optionally filtered by name or pid; sorted by CPU.", READ,
               params={"query": Param("string", "Name fragment or pid", required=False)})
def find_processes(ctx: Context, query: str = "") -> Result:
    rows = system.processes(query, limit=40)
    body = "\n".join(f"{r['pid']:>7} {r['cpu']:>5}% {r['mem']:>5}% {r['rss']:>7} {r['elapsed']:>11}  {r['name']}" for r in rows)
    what = f" matching {query!r}" if query else ""
    return Result.done(f"{len(rows)} process{'es' if len(rows) != 1 else ''}{what}", body, count=len(rows))


@registry.tool("git_status", "Git branch, working-tree status and recent commits for a directory.", READ,
               params={"path": Param("string", "Directory inside a repository", required=False)})
def git_status(ctx: Context, path: str = ".") -> Result:
    target = resolve(ctx, path or ".")
    stop = check_path(ctx, target, "read")
    if stop:
        return stop
    info = system.git(target)
    if not info:
        return Result.done(f"{target} is not inside a git repository", repo=False)
    dirty = len(info["status"].splitlines()) if info["status"] else 0
    body = f"root: {info['root']}\nbranch: {info['branch']}\nremote: {info['remote']}\n\nstatus:\n{info['status'] or '(clean)'}\n\nrecent:\n{info['recent']}"
    return Result.done(f"git: {info['branch']} · {dirty} changed path{'s' if dirty != 1 else ''}", body, repo=True, dirty=dirty)

"""Scheduled work as tools. A task never gets more authority than its creator
has right now, and never more than conservative unless a person grants it with
`lk schedule`."""
from __future__ import annotations

from ..daemon import schedule
from ..kernel.results import Result
from ..kernel.tools import CHANGE, READ, TASKS, Context, Param, registry


@registry.tool("schedule_task", "Schedule a prompt to run later or repeatedly in the background (read-only authority).",
               TASKS, CHANGE, label="schedule", params={
                   "prompt": Param("string", "What to do each run, as a complete standalone instruction"),
                   "every": Param("string", "Repeat interval like 30m, 2h, 1d", required=False),
                   "daily": Param("string", "Daily time HH:MM", required=False),
                   "delay": Param("string", "Run once after e.g. 10m", required=False),
                   "title": Param("string", "Short name", required=False)})
def schedule_task(ctx: Context, prompt: str, every: str = "", daily: str = "", delay: str = "", title: str = "") -> Result:
    if not ctx.interactive:
        return Result.denied("background work cannot create more background work")
    seconds = schedule.parse_every(every) if every else None
    if every and seconds is None:
        return Result.refused(f"could not read interval {every!r}; use forms like 30m, 2h, 1d")
    wait = schedule.parse_every(delay) if delay else None
    try:
        entry = schedule.add(prompt, every=seconds, daily=daily or None, delay=wait, owner=ctx.actor,
                             access="conservative", workspace=str(ctx.workspace), title=title)
    except ValueError as exc:
        return Result.refused(str(exc))
    return Result.done(f"scheduled “{entry['title']}” · {schedule.describe(entry)} · id {entry['id']}", id=entry["id"])


@registry.tool("list_tasks", "List scheduled background tasks.", READ, label="tasks")
def list_tasks(ctx: Context) -> Result:
    rows = [f"{e['id']}  {'on ' if e.get('enabled', True) else 'off'}  {schedule.describe(e):<12} {e.get('owner')}  {e.get('title')}"
            for e in schedule.load()]
    return Result.done(f"{len(rows)} scheduled task{'s' if len(rows) != 1 else ''}", "\n".join(rows))


@registry.tool("cancel_task", "Cancel a scheduled task by id.", TASKS, CHANGE, label="cancel",
               params={"id": Param("string", "Task id")})
def cancel_task(ctx: Context, id: str) -> Result:
    return Result.done(f"cancelled task {id}") if schedule.remove(id) else Result.refused(f"no task {id}")

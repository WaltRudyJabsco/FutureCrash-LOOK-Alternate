"""lk — the terminal language.

Every command below is one registry entry (see faces/commands.py). Output goes
through a few shared helpers so the whole vocabulary looks like one tool.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import List

from .. import __version__, profile as profile_mod, store, system
from ..daemon import jobs, looked, schedule
from ..kernel import fsops, lane
from ..kernel.journal import journal
from ..kernel.ollama import (LOCAL, Ollama, OllamaError, current_host, discover_hosts, load_hosts, normalize_url,
                             resolve_host, save_hosts)
from ..kernel.policy import DESCRIPTIONS, PROFILES, list_grants, revoke_grant
from ..kernel.results import Result
from ..mind import personas, skills
from ..mind.memory import Mind
from ..paths import roots
from ..settings import TABLE, secret, set_secret, settings
from ..term import feedback
from ..term.ansi import style, term_size
from ..term.menu import Item, pick
from . import filer
from .commands import AREAS, commands

command = commands.command
STATUS = {"ok": "ok", "dispatched": "accent", "denied": "warn", "refused": "bad", "failed": "bad", "cancelled": "muted"}


# ---- output helpers ---------------------------------------------------------

def title(text: str, detail: str = "") -> None:
    print(style(text, "title") + (style("  " + detail, "muted") if detail else ""))


def row(label: str, value: object, label_w: int = 14) -> None:
    print(f"  {style(label.ljust(label_w), 'muted')}{value}")


def say(result: Result) -> int:
    print(f"{style(result.mark, STATUS[result.status])} {result.receipt}")
    if result.content and result.ok:
        print(result.content)
    return 0 if result.ok else 1


def bad(text: str) -> int:
    print(style(text, "bad"), file=sys.stderr)
    return 2


def need(args: List[str], n: int, usage: str) -> bool:
    if len(args) < n:
        print(f"usage: {usage}", file=sys.stderr)
        return False
    return True


def page(text: str) -> None:
    if sys.stdout.isatty() and text.count("\n") > term_size()[1] - 2 and shutil.which("less"):
        import subprocess
        subprocess.run(["less", "-R", "-F", "-X"], input=text.encode())
    else:
        print(text, end="" if text.endswith("\n") else "\n")


# ---- files ----------------------------------------------------------------

for _mode in filer.MODES:
    def _view(args, _m=_mode):
        return filer.main(args, mode=_m)
    command(_mode, "[PATH|WORD]", f"interactive {_mode} view", "files")(_view)


def _paths(args: List[str]) -> List[Path]:
    return [Path(os.path.abspath(os.path.expanduser(a))) for a in args]


def _journaled(name: str, result: Result) -> int:
    class _T:
        effect = "change"
    _T.name = name
    journal.record_tool("shell", _T, {}, result)
    if result.undo_id:
        result.receipt += style("  · lk undo", "dark")
    return say(result)


@command("cp", "SRC... DEST", "copy (never overwrites; clashes get ' copy')", "files", aliases=["copy"])
def cmd_cp(args):
    if not need(args, 2, "lk cp SRC... DEST"):
        return 2
    return _journaled("cp", fsops.copy_paths(_paths(args[:-1]), _paths(args[-1:])[0], args[-1]))


@command("mv", "SRC... DEST", "move or rename (refuses to overwrite)", "files", aliases=["move"])
def cmd_mv(args):
    if not need(args, 2, "lk mv SRC... DEST"):
        return 2
    return _journaled("mv", fsops.move_paths(_paths(args[:-1]), _paths(args[-1:])[0], args[-1]))


@command("rm", "PATH...", "remove to LOOK's trash (undoable)", "files", aliases=["remove"])
def cmd_rm(args):
    if not need(args, 1, "lk rm PATH..."):
        return 2
    return _journaled("rm", fsops.remove_paths(_paths(args)))


@command("mk", "PATH | DIR/", "make a file, or a folder when it ends with /", "files", aliases=["make"])
def cmd_mk(args):
    if not need(args, 1, "lk mk PATH  (end with / for a folder)"):
        return 2
    target = _paths(args[:1])[0]
    if args[0].endswith("/"):
        code = _journaled("mk", fsops.make_directory(target))
        if code == 0 and os.environ.get("LOOK_HANDOFF"):
            Path(os.environ["LOOK_HANDOFF"]).write_text(str(target))  # the shell wrapper cds into it
        return code
    return _journaled("mk", fsops.touch(target, parents=True))


@command("undo", "[list|skip|ID]", "reverse the newest change made by LOOK, LO or the filer", "files")
def cmd_undo(args):
    if args and args[0] in ("list", "history"):
        stack = journal.undo_stack()[:30]
        if not stack:
            print(style("nothing in the undo history", "muted"))
        for rec, state in stack:
            color = {"ready": "ok", "undone": "muted", "skipped": "dark"}[state]
            when = time.strftime("%m-%d %H:%M", time.localtime(rec.get("t", 0)))
            print(f"  {style(state.ljust(7), color)} {style(when, 'dark')} {style(rec['actor'].ljust(12), 'muted')} {rec['receipt']}  {style(rec['id'], 'dark')}")
        return 0
    if args and args[0] == "skip":
        return say(journal.skip())
    return say(journal.undo(args[0] if args else None))


@command("journal", "[N]", "what LOOK, LO and background jobs actually did", "files", aliases=["receipts"])
def cmd_journal(args):
    print_journal(int(args[0]) if args and args[0].isdigit() else 30)
    return 0


def print_journal(n: int) -> None:
    rows = [r for r in journal.rows(limit=4000) if r.get("type") in ("tool", "undo", "event")][-n:]
    for r in rows:
        when = style(time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("t", 0))), "dark")
        if r["type"] == "tool":
            mark = Result(bool(r.get("ok")), r.get("status", "failed"), "").mark
            print(f"  {when} {style(mark, STATUS.get(r.get('status'), 'muted'))} {style(r.get('actor', '?').ljust(12), 'muted')} {style(r.get('tool', ''), 'accent')} {r.get('receipt', '')}")
        elif r["type"] == "undo":
            print(f"  {when} {style('↶', 'warn')} {style('undo'.ljust(12), 'muted')} {r.get('receipt', '')}")
        else:
            print(f"  {when} {style('·', 'dark')} {style(r.get('name', ''), 'muted')}")


@command("open", "PATH", "open in its desktop app", "files")
def cmd_open(args):
    from ..tools.host import launch
    return say(launch(_paths(args[:1])[0])) if need(args, 1, "lk open PATH") else 2


@command("preview", "PATH", "quick external preview", "files")
def cmd_preview(args):
    from ..tools.host import launch
    return say(launch(_paths(args[:1])[0], preview=True)) if need(args, 1, "lk preview PATH") else 2


@command("reveal", "PATH", "show in Finder / the file manager", "files")
def cmd_reveal(args):
    from ..tools.host import reveal
    return say(reveal(_paths(args[:1])[0])) if need(args, 1, "lk reveal PATH") else 2


@command("find", "WORD", "find names under your home folder", "files")
def cmd_find(args):
    if not args:
        return bad("usage: lk find WORD")
    if sys.stdin.isatty() and sys.stdout.isatty():
        f = filer.Filer(Path.home(), on_go=lambda p: Path(os.environ["LOOK_HANDOFF"]).write_text(str(p)) if os.environ.get("LOOK_HANDOFF") else None)
        f.find(" ".join(args))
        f.run()
        return 0
    from ..kernel.tools import Context
    from ..tools.files import search_files
    r = search_files(Context(Path.home(), "conservative", interactive=False), " ".join(args), "~")
    return say(r)


# ---- LO, models, hosts --------------------------------------------------------

@command("models", "[NAME]", "choose the model LO and Future Crash share", "ai", aliases=["ollama models"])
def cmd_models(args):
    name, url = current_host()
    client = Ollama(url)
    try:
        models = client.models()
    except OllamaError as exc:
        return bad(str(exc))
    running = {m.get("name") for m in client.running()}
    active = settings.get("ai.model")
    names = [m.get("name") for m in models]
    if args:
        match = next((n for n in names if n == args[0]), None) or next((n for n in names if n.startswith(args[0])), None)
        if not match:
            return bad(f"{args[0]} is not installed on {name}")
        settings.set("ai.model", match)
        print(style(f"✓ active model: {match}", "ok"))
        return 0
    items = []
    for m in sorted(models, key=lambda m: m.get("name", "")):
        caps = client.capabilities(m["name"]) or []
        tags = [t for t in ("tools", "thinking", "vision") if t in caps]
        detail = f"{system.human(m.get('size', 0)):>7}  {' '.join(tags):<22} {'loaded' if m['name'] in running else ''}"
        items.append(Item(m["name"], detail, m["name"], m["name"] == active))
    if not sys.stdout.isatty():
        for it in items:
            print(f"{'*' if it.current else ' '} {it.label}  {it.detail}")
        return 0
    chosen = pick(f"MODELS @ {name}", items, "enter select · esc cancel · models with tools can act; others only talk")
    if chosen:
        settings.set("ai.model", chosen.value)
        print(style(f"✓ active model: {chosen.value}", "ok"))
    return 0


@command("hosts", "[use NAME | add NAME URL | forget NAME | discover]", "where inference runs (local, desktop over Tailscale…)", "ai",
         aliases=["ollama host", "ollama hosts"], complete=lambda: list(load_hosts()["hosts"]) + ["local", "use", "add", "forget", "discover"])
def cmd_hosts(args):
    data = load_hosts()
    if not args:
        title("INFERENCE HOSTS", f"default: {data['default']}")
        entries = [("local", LOCAL)] + [(k, v.get("url", "")) for k, v in data["hosts"].items()]
        for key, url in entries:
            alive = Ollama(url).alive(0.8)
            mark = style("●", "ok") if alive else style("○", "bad")
            cur = style(" default", "accent") if key == data["default"] else ""
            print(f"  {mark} {key:<16} {style(url, 'muted')}{cur}")
        print(style("\n  lk hosts use NAME · add NAME URL · forget NAME · discover", "dark"))
        return 0
    verb = args[0]
    if verb == "use" and len(args) > 1 or verb not in ("add", "forget", "discover", "use") and len(args) == 1:
        target = args[1] if verb == "use" else args[0]
        key, url = resolve_host(target)
        if not url:
            return bad(f"no host named {target}")
        data["default"] = key
        save_hosts(data)
        print(style(f"✓ LO and Future Crash now use {key} ({url})", "ok"))
        return 0
    if verb == "add" and len(args) >= 3:
        data["hosts"][args[1]] = {"url": normalize_url(args[2])}
        save_hosts(data)
        alive = Ollama(args[2]).alive(1.5)
        print(style(f"✓ saved {args[1]} → {normalize_url(args[2])}", "ok") + ("" if alive else style("  (not reachable right now)", "warn")))
        return 0
    if verb == "forget" and len(args) >= 2:
        if data["hosts"].pop(args[1], None) is None:
            return bad(f"no host named {args[1]}")
        if data["default"] == args[1]:
            data["default"] = "local"
        save_hosts(data)
        print(style(f"✓ forgot {args[1]}", "ok"))
        return 0
    if verb == "discover":
        found = discover_hosts()
        if not found:
            print(style("no Ollama servers found on your tailnet", "muted"))
            return 0
        for f in found:
            known = any(v.get("url") == f["url"] for v in data["hosts"].values())
            print(f"  {style('●', 'ok')} {f['name']:<16} {f['url']}{style('  saved', 'dark') if known else ''}")
            if not known:
                data["hosts"][f["name"]] = {"url": f["url"]}
        save_hosts(data)
        return 0
    return bad("usage: lk hosts [use NAME | add NAME URL | forget NAME | discover]")


@command("access", "[conservative|workspace|power|unsafe]", "LO's default permission profile", "ai",
         aliases=["ollama access"], complete=lambda: list(PROFILES))
def cmd_access(args):
    if args:
        try:
            settings.set("lo.access", args[0])
        except ValueError as exc:
            return bad(str(exc))
    current = settings.get("lo.access")
    title("ACCESS", "intelligence is separate from permission")
    for name in PROFILES:
        mark = style("●", "ok") if name == current else " "
        print(f"  {mark} {name:<13} {style(DESCRIPTIONS[name], 'muted')}")
    grants = list_grants()
    if grants:
        print(style("\n  standing grants outside workspaces:", "muted"))
        for g in grants:
            print(f"    {g['mode']:<6} {g['path']}")
        print(style("    revoke with: lk grants revoke PATH", "dark"))
    return 0


@command("grants", "[revoke PATH]", "folders LO may reach outside its workspace", "ai")
def cmd_grants(args):
    if len(args) >= 2 and args[0] == "revoke":
        n = revoke_grant(args[1])
        print(style(f"✓ revoked {n} grant(s)", "ok" if n else "muted"))
        return 0
    grants = list_grants()
    if not grants:
        print(style("no standing grants — LO asks each time it reaches outside its workspace", "muted"))
    for g in grants:
        print(f"  {g['mode']:<6} {g['path']}")
    return 0


@command("personality", "[NAME]", "LO's voice (never its permissions)", "ai", complete=personas.personalities)
def cmd_personality(args):
    names = personas.personalities()
    current = settings.get("lo.personality")
    if args:
        if args[0] not in names:
            return bad(f"unknown personality; choose from {', '.join(names)}")
        settings.set("lo.personality", args[0])
        print(style(f"✓ LO personality: {args[0]}", "ok"))
        return 0
    items = [Item(n, personas.personality_text(n).splitlines()[0].lstrip("# "), n, n == current) for n in names]
    if not sys.stdout.isatty():
        for it in items:
            print(f"{'*' if it.current else ' '} {it.label}  {it.detail}")
        return 0
    chosen = pick("PERSONALITY", items, f"packs live in {roots.personalities} — add your own .md")
    if chosen:
        settings.set("lo.personality", chosen.value)
        print(style(f"✓ LO personality: {chosen.value}", "ok"))
    return 0


@command("thinking", "[light|adaptive|deep]", "reasoning depth", "ai", complete=lambda: ["light", "adaptive", "deep"])
def cmd_thinking(args):
    if args:
        try:
            settings.set("lo.thinking", args[0])
        except ValueError as exc:
            return bad(str(exc))
    print(f"thinking: {style(settings.get('lo.thinking'), 'accent')}  ·  display: {settings.get('lo.think_display')}")
    return 0


@command("web", "[key|status|forget]", "Ollama web-search key (stored machine-locally, 0600)", "ai", aliases=["ollama key"])
def cmd_web(args):
    verb = args[0] if args else "status"
    if verb == "key":
        import getpass
        value = getpass.getpass("Ollama API key (input hidden): ").strip()
        if not value:
            return bad("no key entered")
        set_secret("OLLAMA_API_KEY", value)
        print(style(f"✓ stored in {roots.secrets} (0600); web_search is now offered to LO", "ok"))
        return 0
    if verb == "forget":
        set_secret("OLLAMA_API_KEY", "")
        print(style("✓ web key removed", "ok"))
        return 0
    key = secret("OLLAMA_API_KEY")
    src = "environment" if os.environ.get("OLLAMA_API_KEY") else "secrets file" if key else None
    print(f"web search: {style('ready', 'ok') + style(f'  (key from {src})', 'muted') if key else style('off', 'warn') + style('  lk web key', 'dark')}")
    return 0


@command("tools", "", "every tool LO can be offered, with what it needs", "ai")
def cmd_tools(args):
    from ..kernel.tools import load_builtin_tools
    reg = load_builtin_tools()
    title("TOOLS", "capability → which profiles offer it")
    by_cap = {}
    for t in reg.all():
        by_cap.setdefault(t.capability, []).append(t)
    for cap, tools_ in by_cap.items():
        profiles = [p for p, caps in PROFILES.items() if cap in caps]
        print(f"\n  {style(cap.upper(), 'accent')}  {style(', '.join(profiles), 'muted')}")
        for t in tools_:
            only = style(f"  ({', '.join(sorted(t.personas))} only)", "dark") if t.personas else ""
            avail = "" if t.available is None or t.available() else style("  (unavailable)", "warn")
            print(f"    {t.name:<18} {style(t.effect, 'dark'):<8} {t.description.splitlines()[0][:70]}{only}{avail}")
    return 0


@command("vision", "IMAGE [QUESTION]", "ask a vision model about an image", "ai")
def cmd_vision(args):
    if not need(args, 1, "lk vision IMAGE [QUESTION]"):
        return 2
    from . import lo
    question = " ".join(args[1:]) or "Describe this image."
    return lo.main(["--once", "--conservative", "--image", args[0], question])


@command("benchmark", "[--all]", "speed and tool-calling check for your models", "ai", aliases=["ollama test"])
def cmd_benchmark(args):
    name, url = current_host()
    client = Ollama(url)
    try:
        names = client.model_names() if "--all" in args else [settings.get("ai.model")]
    except OllamaError as exc:
        return bad(str(exc))
    title("BENCHMARK", f"{name} · short prompt, one tool-call probe")
    tool = {"type": "function", "function": {"name": "get_time", "description": "Current time for a city",
                                             "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
    for model in names:
        try:
            t0 = time.time()
            r = client.chat_raw(model, [{"role": "user", "content": "In one sentence: why do terminals still matter?"}],
                                options={"num_predict": 80}, think=False)
            gen = (r.get("eval_count") or 0) / max(1e-6, (r.get("eval_duration") or 1) / 1e9)
            pp = (r.get("prompt_eval_count") or 0) / max(1e-6, (r.get("prompt_eval_duration") or 1) / 1e9)
            load = (r.get("load_duration") or 0) / 1e9
            ok_tools = "—"
            if client.supports(model, "tools"):
                tr = client.chat_raw(model, [{"role": "user", "content": "What time is it in Lisbon? Use the tool."}], tools=[tool], think=False)
                ok_tools = style("calls tools", "ok") if (tr.get("message") or {}).get("tool_calls") else style("did not call", "warn")
            print(f"  {model:<28} {gen:6.1f} tok/s gen  {pp:7.0f} tok/s prompt  load {load:4.1f}s  {ok_tools}  {style(f'{time.time() - t0:.1f}s', 'dark')}")
        except OllamaError as exc:
            print(f"  {model:<28} {style(str(exc), 'bad')}")
    return 0


# ---- the resident coordinator --------------------------------------------------

@command("ai", "[status|start|stop]", "the inference lane, background queue and looked", "ai",
         complete=lambda: ["status", "start", "stop"])
def cmd_ai(args):
    from ..daemon.client import send
    verb = args[0] if args else "status"
    if verb == "start":
        if looked.pid():
            print(style("looked is already running", "muted"))
            return 0
        import subprocess
        subprocess.Popen([sys.executable, "-m", "look", "looked"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=_pyenv())
        for _ in range(30):
            if send({"op": "ping"}, 0.2):
                print(style("✓ looked started", "ok"))
                return 0
            time.sleep(0.1)
        return bad("looked did not start; see lk log")
    if verb == "stop":
        if send({"op": "stop"}) is None:
            print(style("looked is not running", "muted"))
        else:
            print(style("✓ looked stopping", "ok"))
        return 0
    name, url = current_host()
    client = Ollama(url)
    title("AI", "one model configuration, one lane")
    alive = client.alive(1.5)
    row("host", f"{name} {style('●', 'ok') if alive else style('○ unreachable', 'bad')}  {style(url, 'dark')}")
    row("model", settings.get("ai.model") + (style(f"  · reflection: {settings.get('ai.fast_model')}", "muted") if settings.get("ai.fast_model") else ""))
    if alive:
        loaded = [m.get("name") for m in client.running()]
        row("loaded", ", ".join(loaded) or style("none", "muted"))
    row("access", f"LO {settings.get('lo.access')} · Future Crash {settings.get('crash.authority')}")
    row("lane", ", ".join(lane.busy_clients()) or style("idle", "muted"))
    status = send({"op": "status"}, 1.0)
    if status:
        row("looked", f"{style('running', 'ok')} pid {status['pid']} · up {system.duration(status['uptime'])} · done {status['completed']}")
    else:
        row("looked", style("not running", "muted") + style("  (queue drains on demand; schedules need `lk ai start`)", "dark"))
    q = jobs.counts()
    row("queue", f"{q['job']} jobs · {q['reflect']} reflections" + (style(f" · {len(list((roots.queue / 'failed').glob('*.json')))} failed", "warn") if (roots.queue / 'failed').is_dir() and any((roots.queue / 'failed').iterdir()) else ""))
    row("scheduled", str(len([e for e in schedule.load() if e.get("enabled", True)])))
    return 0


def _pyenv() -> dict:
    root = str(Path(__file__).resolve().parents[2])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH", "")])))


@command("looked", "", "run the coordinator in the foreground", "ai", hidden=True)
def cmd_looked(args):
    return looked.run()


@command("_drain", "", "drain the queue once (spawned on demand)", "ai", hidden=True)
def cmd_drain(args):
    handle = store.try_lock(roots.runtime / "drain.lock")
    if handle is None:
        return 0
    try:
        schedule.dispatch_due()
        jobs.drain()
    finally:
        handle.close()
    return 0


# ---- memory & craft --------------------------------------------------------------

def show_memory(mind: Mind) -> None:
    data = mind.load()
    title(f"MEMORY · {mind.name}", f"{len(data['candidates'])}/20 candidates · cycle {data.get('cycles', 0)}")
    print(style("\n  long-term", "accent"))
    for line in (fit_wrap(data.get("summary") or "(empty)", 4)):
        print(line)
    print(style("\n  candidates", "accent") + style("  importance decays by one each reflection unless reinforced", "dark"))
    for c in sorted(data["candidates"], key=lambda c: -int(c.get("importance", 0))):
        bar = "█" * (int(c["importance"]) // 10)
        print(f"    {style(str(c['importance']).rjust(3), 'warn')} {style(bar.ljust(10), 'dark')} {c['text']}")
    recent = mind.recent()
    print(style("\n  recent", "accent") + style(f"  {len(recent)} exchanges kept literally", "dark"))
    for r in recent[-3:]:
        print(f"    {style('you', 'muted')} {r['user'][:90]}")
    q = jobs.counts()["reflect"]
    if q:
        print(style(f"\n  {q} exchange(s) waiting for reflection", "warn"))


def fit_wrap(text: str, indent: int) -> List[str]:
    from ..term.ansi import wrap
    return [" " * indent + l for l in wrap(text, term_size()[0] - indent - 2)]


@command("memory", "[add TEXT | forget TEXT | clear | clear-recent | path] [--mind future-crash]", "what LO remembers about you", "mind",
         complete=lambda: ["add", "forget", "clear", "clear-recent", "path", "--mind"])
def cmd_memory(args):
    mind_name = "lo"
    if "--mind" in args:
        i = args.index("--mind")
        mind_name = args[i + 1] if i + 1 < len(args) else "lo"
        args = args[:i] + args[i + 2:]
    mind = Mind(mind_name)
    if not args:
        show_memory(mind)
        return 0
    verb, rest = args[0], " ".join(args[1:]).strip()
    if verb == "add" and rest:
        with mind.lock():
            data = mind.load()
            outcome = mind.add(data, rest, 85)
            mind.save(data)
        print(style(f"✓ {outcome}", "ok"))
    elif verb == "forget" and rest:
        print(style(f"✓ removed {mind.forget(rest)} item(s)", "ok"))
    elif verb == "clear":
        if _confirm(f"Clear all of {mind_name}'s memory? [y/N] "):
            mind.clear()
            print(style("✓ cleared", "ok"))
    elif verb == "clear-recent":
        mind.clear_recent()
        print(style("✓ recent exchanges cleared", "ok"))
    elif verb == "path":
        print(mind.memory_path)
    else:
        return bad("usage: lk memory [add TEXT | forget TEXT | clear | clear-recent | path]")
    return 0


@command("forget", "TEXT", "forget memories containing TEXT", "mind")
def cmd_forget(args):
    return cmd_memory(["forget"] + args)


@command("skills", "[add TEXT | forget TEXT | clear-learned | update | path]", "how LO does its job (skills.md)", "mind",
         complete=lambda: ["add", "forget", "clear-learned", "update", "path"])
def cmd_skills(args):
    if not args:
        bundled, learned = skills.load()
        version, _ = skills.bundled_pack()
        title("SKILLS", f"bundled pack v{version} · {len(learned)}/{skills.MAX_LEARNED} learned")
        print(style("\n  bundled", "accent"))
        for s in bundled:
            print(f"    • {s}")
        print(style("\n  learned", "accent") + ("" if learned else style("  (none yet)", "dark")))
        for s in learned:
            print(f"    • {s}")
        print(style(f"\n  edit freely: {roots.skills}", "dark"))
        return 0
    verb, rest = args[0], " ".join(args[1:]).strip()
    if verb == "add" and rest:
        print(style(f"✓ {skills.add(rest)}", "ok"))
    elif verb == "forget" and rest:
        print(style(f"✓ removed {skills.forget(rest)}", "ok"))
    elif verb == "clear-learned":
        print(style(f"✓ cleared {skills.clear_learned()} learned skills", "ok"))
    elif verb == "update":
        skills.ensure()
        print(style("✓ bundled craft is current; learned craft untouched", "ok"))
    elif verb == "path":
        print(roots.skills)
    else:
        return bad("usage: lk skills [add TEXT | forget TEXT | clear-learned | update | path]")
    return 0


# ---- machine -----------------------------------------------------------------------

@command("machine", "", "machine summary", "system", aliases=["box"])
def cmd_machine(args):
    info = system.machine()
    title("MACHINE", info["host"])
    for k, v in info.items():
        if k != "host":
            row(k, v, 10)
    g = system.gpu()
    if g:
        row("gpu", g, 10)
    addrs = system.addresses()
    if addrs:
        row("addresses", " ".join(a for a in addrs if not a.startswith("127.")), 10)
    return 0


@command("ports", "", "listening ports and their owners", "system", aliases=["up"])
def cmd_ports(args):
    rows = system.ports()
    title("LISTENING", f"{len(rows)} ports")
    for r in rows:
        print(f"  {style(r['port'].rjust(6), 'accent')}  {r['process']:<22} {style('pid ' + r['pid'], 'muted'):<10}  {style(r['address'], 'dark')}")
    return 0


@command("port", "NUMBER", "who is on a port", "system")
def cmd_port(args):
    if not args or not args[0].isdigit():
        return bad("usage: lk port NUMBER")
    hits = [r for r in system.ports() if r["port"] == args[0]]
    if not hits:
        print(style(f"nothing is listening on {args[0]}", "muted"))
        return 1
    for r in hits:
        print(f"  {r['port']}  {r['process']}  pid {r['pid']}  {r['address']}")
        for p in system.processes(r["pid"], 1):
            print(style(f"       cpu {p['cpu']}% · mem {p['mem']}% · rss {p['rss']} · up {p['elapsed']}", "muted"))
    return 0


@command("processes", "[NAME|PID]", "processes by CPU", "system", aliases=["procs", "process", "proc", "pid"])
def cmd_processes(args):
    rows = system.processes(" ".join(args), 30)
    if not rows:
        print(style("no matching process", "muted"))
        return 1
    print(style(f"  {'PID':>7} {'CPU%':>5} {'MEM%':>5} {'RSS':>7} {'ELAPSED':>11}  NAME", "muted"))
    for r in rows:
        print(f"  {r['pid']:>7} {r['cpu']:>5} {r['mem']:>5} {r['rss']:>7} {r['elapsed']:>11}  {r['name']}")
    return 0


@command("disk", "", "storage", "system")
def cmd_disk(args):
    title("DISK")
    for label, path in (("home", str(Path.home())), ("root", "/")):
        used, total = system.disk(path)
        pct = used / max(1, total)
        bar = "█" * int(pct * 30)
        print(f"  {label:<6} {style(bar.ljust(30, '·'), 'warn' if pct > 0.85 else 'accent')} {system.human(used)} / {system.human(total)} ({pct:.0%})")
    return 0


@command("gpu", "", "graphics / accelerator", "system")
def cmd_gpu(args):
    print(system.gpu() or style("no GPU information available", "muted"))
    return 0


@command("git", "[PATH]", "repository state", "system")
def cmd_git(args):
    info = system.git(Path(args[0] if args else ".").resolve())
    if not info:
        print(style("not inside a git repository", "muted"))
        return 1
    title("GIT", info["root"])
    row("branch", info["branch"], 8)
    row("remote", info["remote"] or style("none", "muted"), 8)
    print(style("\n  status", "accent"))
    print("\n".join("    " + l for l in (info["status"] or "(clean)").splitlines()))
    print(style("\n  recent", "accent"))
    print("\n".join("    " + l for l in info["recent"].splitlines()))
    return 0


@command("run", "[PATH]", "how to run the project here", "system", aliases=["project"])
def cmd_run(args):
    root = Path(args[0] if args else ".").resolve()
    hints = []
    checks = [("package.json", "npm install && npm run dev"), ("pyproject.toml", "pip install -e . / uv run"),
              ("requirements.txt", "pip install -r requirements.txt"), ("Cargo.toml", "cargo run"), ("go.mod", "go run ."),
              ("Makefile", "make"), ("docker-compose.yml", "docker compose up"), ("compose.yaml", "docker compose up"),
              ("Gemfile", "bundle exec"), ("manage.py", "python manage.py runserver")]
    for name, hint in checks:
        if (root / name).exists():
            hints.append((name, hint))
    pkg = store.read_json(root / "package.json", None)
    title("PROJECT", str(root))
    if not hints:
        print(style("  no recognised project files here", "muted"))
    for name, hint in hints:
        print(f"  {style(name.ljust(20), 'accent')} {hint}")
    if isinstance(pkg, dict) and pkg.get("scripts"):
        print(style("\n  npm scripts", "accent"))
        for k, v in pkg["scripts"].items():
            print(f"    {k:<14} {style(str(v)[:70], 'muted')}")
    return 0


@command("env", "[FILTER]", "environment variables (secrets masked)", "system")
def cmd_env(args):
    f = (args[0] if args else "").casefold()
    for k in sorted(os.environ):
        if f and f not in k.casefold():
            continue
        v = os.environ[k]
        if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS")):
            v = v[:3] + "…" if v else ""
        print(f"  {style(k, 'accent')}={v[:200]}")
    return 0


@command("path", "", "PATH entries, in order, and whether they exist", "system")
def cmd_path(args):
    seen = set()
    for i, d in enumerate(os.environ.get("PATH", "").split(os.pathsep), 1):
        mark = style("dup", "warn") if d in seen else (style("✓", "ok") if os.path.isdir(d) else style("missing", "bad"))
        seen.add(d)
        print(f"  {i:>2} {mark:<3} {d}")
    return 0


@command("why", "COMMAND", "how a command name resolves", "system")
def cmd_why(args):
    if not args:
        return bad("usage: lk why COMMAND")
    info = system.which(args[0])
    if not info["resolved"]:
        print(style(f"{args[0]} is not on PATH (it may be a shell alias or function: type {args[0]})", "muted"))
        return 1
    row("runs", info["resolved"], 8)
    if info["real"] != info["resolved"]:
        row("real", info["real"], 8)
    for other in info["all"][1:]:
        row("shadows", other, 8)
    return 0


@command("net", "", "addresses, Tailscale and inference reachability", "net", aliases=["network", "tailscale"])
def cmd_net(args):
    title("NETWORK")
    row("addresses", " ".join(system.addresses()) or "?")
    ts = shutil.which("tailscale")
    row("tailscale", (system.run(["tailscale", "ip", "-4"]).strip() or style("installed, not connected", "warn")) if ts else style("not installed", "muted"))
    name, url = current_host()
    row("inference", f"{name} {url} " + (style("●", "ok") if Ollama(url).alive(1) else style("○", "bad")))
    row("web search", style("ready", "ok") if secret("OLLAMA_API_KEY") else style("off", "muted"))
    return 0


# ---- schedules & jobs ------------------------------------------------------------------

@command("schedule", "[add (--every 2h|--daily 09:00|--in 10m) PROMPT | rm ID | pause ID | resume ID]",
         "prompts that run later or on a rhythm (shared with Future Crash threads)", "time", complete=lambda: ["add", "rm", "pause", "resume"])
def cmd_schedule(args):
    if not args:
        entries = schedule.load()
        title("SCHEDULE", "background runs are unattended: anything needing a person is denied, not guessed")
        if not entries:
            print(style("  nothing scheduled · lk schedule add --every 2h \"summarise new files in ~/Downloads\"", "muted"))
        for e in entries:
            nxt = time.strftime("%a %H:%M", time.localtime(float(e.get("next") or 0))) if e.get("enabled", True) else "paused"
            print(f"  {style(e['id'], 'dark')} {schedule.describe(e):<12} {nxt:<10} {style(e.get('access', ''), 'muted'):<14} {e.get('owner', 'lo'):<13} {e.get('title')}")
        if not looked.pid():
            print(style("\n  schedules fire while looked runs: lk ai start", "warn"))
        return 0
    verb = args[0]
    if verb == "add":
        rest, every, daily, delay, access = args[1:], None, None, None, "conservative"
        while rest and rest[0].startswith("--"):
            flag = rest.pop(0)
            val = rest.pop(0) if rest else ""
            if flag == "--every":
                every = schedule.parse_every(val)
            elif flag == "--daily":
                daily = val
            elif flag == "--in":
                delay = schedule.parse_every(val)
            elif flag == "--access":
                access = val
        prompt = " ".join(rest).strip()
        if not prompt:
            return bad("usage: lk schedule add --every 2h PROMPT")
        try:
            e = schedule.add(prompt, every=every, daily=daily, delay=delay, access=access, workspace=os.getcwd())
        except ValueError as exc:
            return bad(str(exc))
        print(style(f"✓ scheduled {e['id']} · {schedule.describe(e)} · {access}", "ok"))
        return 0
    if verb in ("rm", "remove") and len(args) > 1:
        return 0 if schedule.remove(args[1]) else bad(f"no entry {args[1]}")
    if verb in ("pause", "resume") and len(args) > 1:
        return 0 if schedule.update(args[1], enabled=verb == "resume") else bad(f"no entry {args[1]}")
    return bad("usage: lk schedule [add ... | rm ID | pause ID | resume ID]")


@command("jobs", "[N]", "background LO results", "time")
def cmd_jobs(args):
    q = jobs.counts()
    title("JOBS", f"{q['job']} queued · {q['reflect']} reflections waiting")
    done = [e for e in journal.events(limit=2000) if e.get("name") in ("job.done", "job.failed")][-int(args[0] if args and args[0].isdigit() else 8):]
    for ev in done:
        rep = (ev.get("data") or {}).get("report") or {}
        when = time.strftime("%m-%d %H:%M", time.localtime(ev.get("t", 0)))
        if ev["name"] == "job.failed":
            print(f"\n  {style(when, 'dark')} {style('failed', 'bad')} {ev['data'].get('error')}")
            continue
        print(f"\n  {style(when, 'dark')} {style(rep.get('title') or rep.get('prompt', '')[:60], 'accent')}  {style(rep.get('ledger', ''), 'muted')}")
        for line in fit_wrap(rep.get("answer", ""), 4)[:12]:
            print(line)
    return 0


@command("_events", "", "print unseen background completions (shell prompt hook)", "time", hidden=True)
def cmd_events(args):
    stamp = roots.runtime / "events.seen"
    try:
        since = stamp.stat().st_mtime
    except OSError:
        since = time.time() - 60
    fresh = [e for e in journal.events(since=since) if e.get("name") in ("job.done", "job.failed")
             and ((e.get("data") or {}).get("report") or {}).get("owner", "lo") == "lo"]
    for ev in fresh:
        rep = (ev.get("data") or {}).get("report") or {}
        ok = ev["name"] == "job.done"
        print(style(f"◆ LO finished: {rep.get('title') or rep.get('prompt', '')[:50]} · {rep.get('ledger', '')} · lk jobs", "accent" if ok else "bad"))
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return 0


# ---- settings & profile ------------------------------------------------------------------

@command("settings", "", "every preference in one place", "config", aliases=["config"])
def cmd_settings(args):
    if not sys.stdout.isatty() or args:
        for s in TABLE:
            v = settings.get(s.key)
            print(f"{s.key:<22} {str(v):<14} {s.help}")
        print(f"\nfiles: profile {roots.profile} · machine {roots.machine} · runtime {roots.runtime}")
        return 0

    def items():
        return [Item(s.key, f"{str(settings.get(s.key)):<16} {s.help}", s, group=s.key.split('.')[0]) for s in TABLE]

    def cycle(key, item):
        spec = item.value
        cur = settings.get(spec.key)
        if spec.kind is bool:
            settings.set(spec.key, not cur)
        elif spec.choices:
            opts = list(spec.choices)
            settings.set(spec.key, opts[(opts.index(cur) + (1 if key != "left" else -1)) % len(opts)])
        item.detail = f"{str(settings.get(spec.key)):<16} {spec.help}"
        return None

    while True:
        chosen = pick("SETTINGS", items(), "←/→ change · enter edit text/number · esc done", on_key=cycle)
        if not chosen:
            return 0
        spec = chosen.value
        if spec.kind is bool or spec.choices:
            cycle("right", chosen)
            continue
        try:
            value = input(f"{spec.key} [{settings.get(spec.key)}]: ").strip()
            if value:
                settings.set(spec.key, value)
        except (ValueError, EOFError, KeyboardInterrupt) as exc:
            print(style(str(exc), "bad"))
            time.sleep(1)


@command("set", "KEY VALUE", "change one setting", "config", complete=lambda: [s.key for s in TABLE])
def cmd_set(args):
    if len(args) < 2:
        return bad("usage: lk set KEY VALUE   (see lk settings)")
    try:
        value = settings.set(args[0], " ".join(args[1:]))
    except (KeyError, ValueError) as exc:
        return bad(str(exc))
    print(style(f"✓ {args[0]} = {value}", "ok"))
    return 0


@command("profile", "[backup DEST | export FILE | restore SOURCE | import-3x [--dry-run]]", "your portable LOOK", "config",
         complete=lambda: ["backup", "export", "restore", "import-3x"])
def cmd_profile(args):
    verb = args[0] if args else "status"
    try:
        if verb == "status":
            st = profile_mod.status()
            title("PROFILE", "memory, skills, core, personalities, settings, schedules")
            for k, v in st.items():
                row(k, system.human(v) if k == "bytes" else v, 12)
            legacy = roots.legacy_state
            if (legacy / "ollama_memory.json").exists():
                print(style(f"\n  LOOK 3.x state found at {legacy} · lk profile import-3x", "warn"))
        elif verb == "backup":
            keep = None
            if "--keep" in args:
                i = args.index("--keep")
                keep = int(args[i + 1])
                args = args[:i] + args[i + 2:]
            print(style(f"✓ {profile_mod.backup(args[1] if len(args) > 1 else None, keep)}", "ok"))
        elif verb == "export":
            print(style(f"✓ {profile_mod.export(args[1] if len(args) > 1 else None)}", "ok"))
        elif verb == "restore" and len(args) > 1:
            n, safety = profile_mod.restore(args[1])
            print(style(f"✓ restored {n} files · previous profile kept at {safety}", "ok"))
        elif verb in ("import-3x", "import"):
            for note in profile_mod.import_3x(dry_run="--dry-run" in args):
                print(f"  · {note}")
        else:
            return bad("usage: lk profile [backup DEST | export FILE | restore SOURCE | import-3x]")
    except (ValueError, OSError) as exc:
        return bad(str(exc))
    return 0


@command("apps", "[CATEGORY APP]", "which desktop app opens video, audio, image, document, text", "config")
def cmd_apps(args):
    from ..tools.host import CATEGORIES, app_for, set_app
    if len(args) >= 2:
        if args[0] not in CATEGORIES and args[0] != "folder":
            return bad(f"categories: {', '.join(CATEGORIES)}")
        set_app(args[0], " ".join(args[1:]))
    for cat in list(CATEGORIES) + ["folder"]:
        row(cat, app_for(cat), 10)
    return 0


@command("feedback", "[sound on|off | motion off|subtle|normal | demo]", "semantic sound and motion", "config", aliases=["sound"])
def cmd_feedback(args):
    if len(args) >= 2 and args[0] in ("sound", "motion"):
        try:
            settings.set(f"feedback.{args[0]}", args[1])
        except ValueError as exc:
            return bad(str(exc))
    elif args and args[0] == "demo":
        for ev in feedback.TONES:
            print(f"  {ev}")
            feedback.play(ev, force=True)
            time.sleep(0.45)
        return 0
    row("sound", "on" if settings.get("feedback.sound") else "off", 8)
    row("motion", settings.get("feedback.motion"), 8)
    return 0


@command("shell-init", "zsh|bash", "print shell integration (eval it from your rc file)", "config", complete=lambda: ["zsh", "bash"])
def cmd_shell_init(args):
    from ..shell import script
    lk_path = shutil.which("lk") or str(Path.home() / ".local" / "bin" / "lk")
    real = os.environ.get("LOOK_BIN") or lk_path
    print(script(args[0] if args else "zsh", real))
    return 0


# ---- LOOK itself ---------------------------------------------------------------------------

@command("help", "[all]", "the few entrances worth remembering", "meta")
def cmd_help(args):
    if args and args[0] == "all":
        return cmd_commands(["--print"])
    print(style("LOOK", "title") + style(f" {__version__} · Future Crash + LOOK", "muted"))
    pairs = [("future-crash", "the place you inhabit", "lo", "talk to LO"),
             ("lk", "look at a folder", "lk undo", "reverse the last change"),
             ("lk machine", "machine, ports, processes", "lk ai", "models, hosts, lane, queue"),
             ("lk memory", "what LO remembers", "lk skills", "how LO works"),
             ("lk settings", "every preference", "lk commands", "everything, searchable")]
    print()
    for a, da, b, db in pairs:
        print(f"  {style(a.ljust(13), 'accent')} {da:<28} {style(b.ljust(12), 'accent')} {db}")
    print()
    print(style("  Access: conservative < workspace < power < unsafe · every change is journaled and undoable", "dark"))
    return 0


@command("commands", "", "searchable palette of every command", "meta")
def cmd_commands(args):
    cmds = commands.all()
    if "--print" in args or not sys.stdout.isatty():
        for area, label in AREAS:
            group = [c for c in cmds if c.area == area]
            if not group:
                continue
            print(style(f"\n{label}", "title"))
            for c in group:
                alias = style(f"  ({', '.join(' '.join(a) for a in c.aliases)})", "dark") if c.aliases else ""
                print(f"  {('lk ' + c.name + ' ' + c.usage).strip():<52} {style(c.summary, 'muted')}{alias}")
        print(style("\nlo [--conservative|--workspace|--power|--unsafe] [@host] [search] [PROMPT]  ·  lo bg PROMPT  ·  future-crash", "muted"))
        return 0
    items = [Item(f"lk {c.name} {c.usage}".strip(), c.summary, c, group=c.area) for c in cmds]
    chosen = pick("COMMANDS", items, "enter puts the command on your prompt · esc cancel")
    if chosen:
        cmd = chosen.value
        handoff = roots.handoff / "command"
        store.write_text(handoff, f"lk {cmd.name} ", private=True)
        print(f"lk {cmd.name} {style(cmd.usage, 'muted')}")
    return 0


@command("version", "", "print version", "meta", aliases=["--version", "-V"])
def cmd_version(args):
    print(f"LOOK {__version__}")
    return 0


@command("log", "[N]", "the diagnostic log", "meta")
def cmd_log(args):
    n = int(args[0]) if args and args[0].isdigit() else 40
    lines = store.read_text(roots.log).splitlines()[-n:]
    print("\n".join(lines) if lines else style("log is empty", "muted"))
    return 0


@command("doctor", "", "check everything LOOK depends on", "meta")
def cmd_doctor(args):
    checks = []

    def check(label, ok, detail="", hint=""):
        checks.append(ok)
        mark = style("✓", "ok") if ok is True else style("!", "warn") if ok is None else style("✗", "bad")
        print(f"  {mark} {label:<22} {detail}" + (style(f"  → {hint}", "dark") if hint and ok is not True else ""))

    title("DOCTOR", f"LOOK {__version__}")
    check("python", sys.version_info >= (3, 9), sys.version.split()[0], "LOOK needs Python 3.9+")
    for layer in ("profile", "machine", "runtime"):
        p = getattr(roots, layer)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".doctor"
            probe.write_text("ok")
            probe.unlink()
            check(f"{layer} writable", True, str(p))
        except OSError as exc:
            check(f"{layer} writable", False, str(exc))
    name, url = current_host()
    client = Ollama(url)
    alive = client.alive(2)
    check("ollama", alive, f"{name} {url}", "ollama serve · or lk hosts")
    if alive:
        model = settings.get("ai.model")
        names = client.model_names()
        check("model installed", model in names, model, f"ollama pull {model} · or lk models")
        if model in names:
            check("model uses tools", client.supports(model, "tools") or None, "", "LO can talk but not act with this model")
    check("web search", bool(secret("OLLAMA_API_KEY")) or None, "", "optional: lk web key")
    check("looked", bool(looked.pid()) or None, "running" if looked.pid() else "not running", "optional: lk ai start (needed for schedules)")
    failed = roots.queue / "failed"
    n_failed = len(list(failed.glob("*.json"))) if failed.is_dir() else 0
    check("background queue", n_failed == 0 or None, f"{sum(jobs.counts().values())} pending · {n_failed} failed", "see lk log")
    rc = Path.home() / (".zshrc" if "zsh" in os.environ.get("SHELL", "zsh") else ".bashrc")
    check("shell integration", "shell-init" in store.read_text(rc) or None, str(rc), 'add: eval "$(lk shell-init zsh)"')
    check("chafa (image preview)", bool(shutil.which("chafa")) or None, "", "optional")
    check("tailscale", bool(shutil.which("tailscale")) or None, "", "optional: remote inference")
    if (roots.legacy_state / "ollama_memory.json").exists():
        check("3.x profile", None, str(roots.legacy_state), "lk profile import-3x")
    bad_count = sum(1 for c in checks if c is False)
    print(style(f"\n  {bad_count} problem(s)" if bad_count else "\n  healthy", "bad" if bad_count else "ok"))
    return 1 if bad_count else 0


@command("uninstall", "", "remove the program (your profile stays unless you say so)", "meta")
def cmd_uninstall(args):
    home = Path.home()
    targets = [home / ".local" / "share" / "look" / "look.pyz"] + [home / ".local" / "bin" / n for n in ("lk", "lo", "future-crash")]
    print("This removes the LOOK program files:")
    for t in targets:
        print(f"  {t}")
    if not _confirm("Continue? [y/N] "):
        return 0
    from ..daemon.client import send
    send({"op": "stop"})
    for t in targets:
        try:
            t.unlink()
        except OSError:
            pass
    if _confirm(f"Also delete your profile, machine and runtime state ({roots.profile.parent})? [y/N] "):
        for p in (roots.profile, roots.machine, roots.runtime):
            shutil.rmtree(p, ignore_errors=True)
    print(style("✓ removed · remove the `eval \"$(lk shell-init ...)\"` line from your shell rc", "ok"))
    return 0


@command("_complete", "", "completion words", "meta", hidden=True)
def cmd_complete(args):
    cmd, rest = commands.resolve(args) if args else (None, [])
    if cmd and cmd.complete and not rest:
        print("\n".join(cmd.complete()))
    else:
        print("\n".join(w for w in commands.words(args) if not w.startswith("_")))
    return 0


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ---- entry ----------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    from .. import extras
    extras.load()
    personas.ensure_profile()
    fsops.BASE.set(Path.cwd())
    if not argv:
        return filer.main([])
    cmd, rest = commands.resolve(argv)
    if cmd is None:
        # `lk PATH` or `lk WORD`: the filer's smart jump
        if len(argv) == 1 and not argv[0].startswith("-"):
            return filer.main(argv)
        print(style(f"lk: unknown command {' '.join(argv)!r} — try lk commands", "bad"), file=sys.stderr)
        return 2
    try:
        return int(cmd.fn(rest) or 0)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0

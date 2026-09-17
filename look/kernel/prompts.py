"""System prompt assembly.

Short sections with one job each, in a fixed order. The contract section is
the only place tool discipline is stated; there are no per-verb special cases.
"""
from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import List, Optional

from .policy import DESCRIPTIONS, capabilities
from .tools import EXEC, WRITE

CONTRACT = """HOW THIS TERMINAL WORKS
- Keep answers concise and terminal-readable. Light Markdown is rendered: headings, **bold**, *italics*, lists, `code`, fenced blocks, [label](https://url) links.
- Conversation is valid on its own. Tools being available is never a reason to use one; use them when the request involves files, the machine, current facts, or an action.
- The host shows the user a receipt line for every tool call, written from the tool's result. Treat results as ground truth:
  * never say something was created, changed, opened or found unless a tool result says so in this turn;
  * a denied, refused or failed result means it did not happen — say so plainly and why;
  * "dispatched" means handed to another program, not that it is open or playing;
  * don't repeat receipts back verbatim; add what the user needs beyond them.
- Explaining how you would do something is not doing it. If the user asks for an action, call the tool.
- Paths: relative paths are inside the workspace; "~/Downloads/x" means exactly that. If a path is outside the workspace, call the tool anyway — the host asks the user for permission. Never quietly pick a different destination.
- Copy/move/rename/remove use their own tools; never recreate a file by reading and rewriting it. Several new files: one create_files call.
- Information edges before general web search: weather, place_lookup, wikipedia, wikidata, papers, archive. Results carry provenance (DIRECT, DERIVED, SEARCHED). Keep sources compact in the answer, cite exact URLs from results, never invent URLs or confidence percentages.
- Memory is maintained by the host after the conversation. If asked to remember something, acknowledge it naturally."""


def environment(workspace: Path, selected: Optional[List[Path]] = None) -> str:
    lines = [
        "ENVIRONMENT",
        f"- now: {time.strftime('%A %Y-%m-%d %H:%M %Z')}",
        f"- system: {platform.system()} {platform.machine()}",
        f"- workspace: {workspace}",
    ]
    try:
        entries = sorted(workspace.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        visible = [p for p in entries if not p.name.startswith(".")]
        shown = ", ".join(p.name + ("/" if p.is_dir() else "") for p in visible[:40])
        more = f" (+{len(visible) - 40} more)" if len(visible) > 40 else ""
        lines.append(f"- workspace top level: {shown}{more}" if shown else "- workspace is empty")
    except OSError:
        lines.append("- workspace is not readable")
    if selected:
        lines.append("SELECTED PATHS (handed over as the working set; contents not yet read):")
        for p in selected[:200]:
            try:
                shown = str(p.relative_to(workspace)) or p.name
            except ValueError:
                shown = str(p)
            lines.append(f"  - {shown}{'/' if p.is_dir() else ''}")
    return "\n".join(lines)


def access_note(access: str, tools_supported: bool) -> str:
    if not tools_supported:
        return "ACCESS: this model cannot call tools. You cannot read files or act on the machine; say so if asked."
    caps = capabilities(access)
    text = f"ACCESS PROFILE: {access.upper()} — {DESCRIPTIONS[access]}."
    if WRITE not in caps:
        text += " You cannot change files; if asked, explain which profile would allow it (lo --workspace)."
    if EXEC not in caps:
        text += " Shell commands are unavailable in this profile."
    elif access == "power":
        text += " Every shell command is shown to the user for confirmation first."
    return text


def assemble(persona, access: str, workspace: Path, query: str, tools_supported: bool,
             selected: Optional[List[Path]] = None, memory_enabled: bool = True, extra: str = "") -> str:
    parts = [persona.identity(), CONTRACT, access_note(access, tools_supported)]
    craft = persona.craft()
    if craft:
        parts.append(craft)
    if memory_enabled:
        ctx = persona.mind.context(query)
        if ctx:
            parts.append(ctx)
    parts.append(environment(workspace, selected))
    if extra:
        parts.append(extra)
    return "\n\n".join(p for p in parts if p)

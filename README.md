# FUTURE CRASH + LOOK 4

**A local-first AI terminal environment for macOS and Linux.**
Python 3.9+ standard library only. Your models, through Ollama, on your machine or one you trust.

```text
future-crash     the place you inhabit
lo               the working AI
lk               the terminal language underneath both
```

4.0 is a rewrite. It keeps every idea that made 3.x worth using and replaces the
machinery that was fighting it: one 12,000-line script, two separate AI systems,
and a truth layer that guessed at English. See [docs/WHY-4.md](docs/WHY-4.md).

---

## Install

```sh
cd future-crash-look-4.0.0
./install.sh
exec zsh
future-crash
```

The installer puts the program in `~/.local/share/look/program/4.0.0/` (the previous
version stays next to it for rollback), writes three tiny launchers to `~/.local/bin`,
and adds one block to your shell rc:

```sh
eval "$(lk shell-init zsh)"
```

If it finds LOOK 3.x, it offers to replace the old 774-line shell block and to import
your memory, learned skills, settings, hosts and web key. Nothing from 3.x is deleted.

---

## The idea in one picture

```text
 faces     future-crash      lo           lk · filer
              │               │               │
 kernel    agent loop ── tool registry ── policy ── journal ── lane
              │               │                         │
 mind      personas · memory · skills · reflection      │
              │                                         │
 tools     files · host · shell · inspect · edges · signal · tasks
              │
           Ollama (local, or any host on your tailnet)
```

Everything a face does goes through one kernel. Future Crash's Oracle and LO are two
personas of the same system: same tools, same permission table, same journal, same
inference lane. They keep separate temperaments and separate memories.

---

## Future Crash

```sh
future-crash        # also: fcr, rst
```

An ambient workstation: clock, live telemetry, matrix rain behind a **Signal Field** the
Oracle can draw on, fortunes, observations, the occasional incident.

```text
A ask the Oracle      X workstation (a conversation)    T threads (scheduled work)
F fortune             R observe       S clear signal    D signal demo
M sound               P panic         ? help            Esc  the shell underneath
```

The Oracle draws with a real tool now. It gets a structured receipt back (accepted,
rejected, clipped, bounds, frames) and improves its next picture from that. Its
authority is `crash.authority`, which is conservative by default. Anything beyond that
opens a permission panel on screen.

## LO

```sh
lo                                   # session in this folder
lo "what's eating my disk?"          # answer, then stay
lo search "latest Ollama release"    # prefer web search
lo @desk --power                     # another host, another profile, this session only
lo bg "summarise new files in ~/Downloads"
```

Every tool call prints a receipt written by the host. Every turn ends with a **ledger**
computed from what actually ran:

```text
you › move the drafts into archive/
  ✓ move path moved 3 items → archive
lo ›
  Done. The three drafts are in archive/.
  ✓ 3 changes · lk undo · 1.8s · 42 tok/s
```

If a model says "done" without calling a tool, the ledger reads `◦ no tools used`. No
English was parsed to work that out.

Inside a session: `/access`, `/model`, `/think`, `/undo`, `/receipts`, `/memory`, `/clear`, `/exit`.

## LOOK

```sh
lk                  # interactive filer: type to filter, Tab to mark, C/M/R to copy/move/remove
lk ~/Projects       # jump there   ·   l WORD  jumps to a matching folder and cds your shell on G
lk tree | recent | size | dirs | files
lk cp a b/  ·  lk mv a b  ·  lk rm a  ·  lk mk notes/   (every one undoable)
lk undo             # reverses the newest change by you, the filer, LO or a job
lk machine · lk ports · lk port 11434 · lk processes ollama · lk git · lk why python3
lk commands         # searchable palette of everything
```

---

## Permission

Intelligence is separate from permission.

| Profile | LO may |
|---|---|
| **conservative** | read, search, reason, draw. No changes. |
| **workspace** | change files inside the folder it started in. Anywhere else, it asks (once, for the session, or always). |
| **power** | workspace, plus shell commands. Each one is shown to you first. |
| **unsafe** | unconfirmed shell commands with your privileges. Named for what it is. |

A tool the profile does not allow is **never offered** to the model, and it is refused
again if a model names it anyway. `~/.ssh`, `~/.aws`, keychains, LOOK's own grants and
secrets, and system directories are unreachable in every profile.

## Truth

A tool returns a `Result`: `ok · status · receipt · data`. Status is one of `ok`,
`dispatched`, `denied`, `refused`, `failed` or `cancelled`. The terminal shows the
receipt verbatim, and the model receives the same data. Handing a video to VLC is
`dispatched`, not "playing", because that is all the host observed.

## Undo

Removals go to LOOK's trash. Overwrites keep the previous version. Moves refuse to
clobber. Every change lands in one append-only journal with a reversal recipe, and a
reversal only runs if the world still matches what the change left behind:

```sh
lk undo list
lk undo
lk journal
```

## Memory: small, selective, forgetful

```text
recent        the last few exchanges, literally        (continuity across sessions)
candidates    ≤ 20 memories, importance 0–100           (decay unless reinforced)
long-term     one paragraph ≤ 180 words                 (rewritten, not appended)
```

- At most eight memories reach a prompt, chosen for relevance.
- Retrieval is not reinforcement.
- Reflection is **one** model call per exchange, using Ollama structured JSON output, and it runs in the background lane.
- "Remember this" can never silently vanish.
- Nothing is rewritten without new evidence.

```sh
lk memory                 lk memory forget "old address"
lk skills                 lk skills add "Preview destructive batch operations first"
```

Skills are how LO does its job, kept in an editable `skills.md`. Bundled craft is
refreshed by upgrades, and learned craft is yours.

## One lane, many clients

A person waiting always wins. LO's interactive turns hold a lease. Background jobs,
reflection and scheduled threads run only when no one holds one. Future Crash's ambient
musings run only when the lane is idle *and* the queue is empty. The queue is plain JSON
files, so `looked` (the optional coordinator) can crash or be absent without anything
being lost.

```sh
lk ai             # host, model, lane, queue, looked
lk ai start       # needed for schedules; the installer can start it at login
lk schedule add --every 2h "list anything new in ~/Downloads"
```

## Information edges

Named sources before generic search, each with provenance (`DIRECT`, `DERIVED` or
`SEARCHED`):

`weather` · `place_lookup` · `wikipedia` · `wikidata` · `papers` (Crossref) · `archive` (Internet Archive) · `web_search` (with `lk web key`)

## Portable profile

```text
program   replaceable       ~/.local/share/look/program/
profile   your LOOK         ~/.local/share/look/profile/     memory · skills · core · personalities · settings · schedules
machine   this computer     ~/.config/look/                  hosts · grants · app choices · secrets (0600)
runtime   disposable        ~/.local/state/look/             journal · trash · queue · leases · cache
```

```sh
lk profile backup ~/Documents/LOOK     lk profile export     lk profile restore look-profile.zip
```

## Models and hosts

```sh
lk models                 # one active model, shared by LO and Future Crash
lk hosts discover         # find Ollama across your tailnet
lk hosts use desk
lk benchmark --all        # speed and a real tool-calling probe
lk set ai.fast_model qwen3:1.7b     # optional small model for background reflection
```

## Extending

A tool is one decorated function. An extra is a module with `register(commands, tools)`,
dropped into `look/extras/` or `~/.local/share/look/profile/extras/`. See
[docs/EXTENDING.md](docs/EXTENDING.md).

## Development

```sh
python3 -m unittest discover -s tests      # 53 tests, a scripted fake Ollama, no GPU
LOOK_HOME=/tmp/look-sandbox python3 -m look lo    # a completely separate LOOK
LOOK_DEBUG=1 lk ...                        # tracebacks instead of friendly errors
lk log                                     # the diagnostic log (nothing is silently swallowed)
```

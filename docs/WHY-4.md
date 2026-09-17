# Why 4.0 is a rewrite

3.2 worked, and its ideas were good. The problems were structural, and each release
spent more effort holding the structure up.

## What stayed, because it was right

| 3.x principle | Where it lives in 4.0 |
|---|---|
| Intelligence is separate from permission | `kernel/policy.py`: one table, enforced before offering and again at invocation |
| Tool receipts are ground truth | `kernel/results.py`: `Result` is data; the host renders the receipt |
| File changes are undoable | `kernel/fsops.py` + `kernel/journal.py` |
| Program / Profile / Machine / Runtime | `paths.py`, the only module that knows where anything lives |
| Small, forgetful memory; retrieval ≠ reinforcement | `mind/memory.py` (same limits: 20 / 8 / 180 words / 12 cycles) |
| User memory ≠ assistant craft | `mind/skills.py` and `skills.md` with Bundled/Learned |
| Human-readable, file-shaped intelligence | JSON and Markdown, schema-versioned |
| Few named information edges with provenance | `tools/edges.py` |
| One AI configuration, a lane where people win | `settings.py` (`ai.model`) + `kernel/lane.py` |
| Optional infrastructure fails softly | lane leases and job files work with or without `looked` |
| Semantic feedback, silent when piped | `term/feedback.py` |
| Future Crash's temperament and Signal Field | `faces/crash/`, `bundled/future-crash.md` |

## What changed, and why

**One 12,201-line script → a package of small modules.** The coordinator used to load
`lk` as a module through `SourceFileLoader`, so upgrades had to stop it first. Now
every process imports the same package.

**Two AI systems → one kernel with two personas.** Future Crash had its own tool
protocol (`[[TOOL]]`), permission model, memory store, scheduler, audio engine and
Ollama client. Now it uses LO's registry, policy, journal, scheduler and lane, and
keeps only what makes it Future Crash.

**Truth by guessing → truth by construction.** 3.x decided whether you had asked for
a file change by matching verbs, flagged "now playing" in model output, and rewrote
"is now playing in VLC" into "was handed to the VLC launcher". Releases 2.10.1–3.1.0
were mostly fixes to that layer. In 4.0 the model never reports outcomes the terminal
relies on. The host prints receipts from Results and a ledger from the turn. None of
it depends on the model's language or phrasing.

**Keyword budgets → structural budgets.** The effort tier and the "needs thinking"
decision came from lists like `"debug", "analy", "architect"`. Now tool rounds start
small and grow while rounds make progress. Context size follows the actual message
size. Adaptive thinking looks at structure: length, code blocks, multiple lines,
selected files, a failed tool.

**No argument rewriting.** 3.x replaced the model's chosen path when your prompt
contained "my Downloads folder". Now the path means what it says. If it is outside the
workspace, the host asks you.

**Five reflection formats → one JSON schema.** Candidate extraction, semantic
matching, skill extraction and feedback reflection were separate calls, each with its
own line format (`NN|text`, `SKILL|…`). Now there is one structured call, plus a
summary rewrite only when one is due.

**Separate state files → one journal.** Receipts, undo, skipped-undo and events are
lines in `journal.jsonl`, and undo state is derived from it.

**A dozen tiny preference files → `settings.json`**, declared once in a table that
also drives `lk settings`, `lk set` and completion.

**774 lines of zshrc → `eval "$(lk shell-init zsh)"`**, generated so it cannot drift.
It never overwrites a name you already use.

**Built-in audits → tests.** `architecture-audit`, `tooling-audit` and `locator-test`
became 53 unit tests against a scripted fake Ollama, passing on Python 3.9, 3.10 and 3.11.

**Broad `except Exception: pass` → boundaries that log.** Catch-alls remain only where
a failure must not spread (a tool run, a UI frame, a job), and they write to `lk log`.

**Scope → extras.** Media control is an extra that registers its own command. ComfyUI,
the games, translation and tailnet sharing proxies are the obvious next extras. They
did not belong in the core file.

## Size

| | 3.2 | 4.0 |
|---|---|---|
| Python | ~18,300 lines in 5 files | ~9,000 lines in 45 modules |
| Shell integration | 774-line zshrc + 494-line installer | generated (~90 lines) + installer |
| Tests | 0 | 53 |
| Version numbers | release, LOOK, Future Crash, Living AI | one |

# Architecture

```text
look/
  paths.py            the four state layers (program · profile · machine · runtime)
  store.py            atomic JSON, bounded JSON lines, file locks — the only writer of state
  settings.py         declarative settings table + machine-local secrets
  log.py              the diagnostic log
  system.py           read-only machine facts (macOS + Linux)
  profile.py          backup · export · restore · import from 3.x
  shell.py            generated shell integration

  kernel/
    results.py        Result: ok · status · receipt · content · data · undo
    tools.py          Tool declarations, Registry, Context, invoke() — the one transaction boundary
    policy.py         profiles → capabilities, path boundary, grants, command confirmation
    fsops.py          reversible copy / move / remove / write / create / mkdir
    journal.py        append-only journal; undo derived from it
    ollama.py         the only Ollama client; hosts; tailnet discovery; web search
    lane.py           interactive leases; background work waits for people
    prompts.py        system prompt assembly (identity · contract · access · craft · memory · environment)
    agent.py          the agent loop, budget, ledger

  mind/
    personas.py       LO and Future Crash: voice + private mind, never permission
    memory.py         recent ring · candidates with decay · long-term summary
    reflect.py        one structured reflection per exchange; summary rewrite when due
    skills.py         skills.md, Bundled + Learned

  tools/              files · host · shell · inspect · edges · signal · tasks
  daemon/
    jobs.py           durable file queue; drain() runs identically in looked or a one-shot drainer
    schedule.py       one scheduler for LO schedules and Future Crash threads
    looked.py         optional resident: fires schedules, drains the queue, answers status
    client.py         Unix-socket client

  faces/
    commands.py       the lk command registry
    cli.py            every lk command
    lo.py             the LO REPL and terminal UI
    filer.py          the interactive filer
    crash/app.py      Future Crash
    crash/canvas.py   the Signal canvas and its drawing language
    crash/text.py     fortunes, observations, panics, incidents

  term/               ansi · keys · markdown · menu · feedback
  extras/             optional features: register(commands, tools)
  bundled/            core.md · skills.md · personalities · future-crash.md
```

## A turn, end to end

1. A face builds an `Agent(persona, access, workspace, ui)`.
2. `turn(text)` takes an interactive lease on the lane. Ambient work opts out with
   `lease=False`, so it never blocks background jobs.
3. `prompts.assemble` builds one system prompt: identity, contract, access note, craft,
   memory context (≤8 relevant memories plus the summary and recent exchanges), and
   environment.
4. Tools are offered only if the model supports tools **and** the profile allows the
   capability **and** the persona may use the tool **and** it is available (for
   example, web search has a key).
5. A tool call goes through `invoke()`: policy check → argument check → run → `Result`
   → journal. The UI's `receipt()` renders it. The model gets `result.for_model()`.
6. Identical calls are refused after two. Rounds start at 4 and grow by 4 while new calls
   succeed, up to `lo.max_rounds`. The last round offers no tools.
7. The turn returns `Turn(answer, results, ledger())`. Recent continuity is written now,
   and reflection is queued.

## Background work

```text
enqueue(kind, payload) ──► runtime/queue/<kind>/<id>.json
        │
        ├─ looked running?  ──► wake it
        └─ else             ──► spawn `python -m look _drain` (single instance via lock)

drain(): while files exist → wait for lane.idle() → run_one() → journal event
         failure → attempts+1, 30 s backoff; after 5 → queue/failed/
```

Jobs run with `interactive=False`. Anything that would ask a person is **denied**, never guessed.

## Dependencies between layers

`faces → mind, tools, daemon → kernel → store, paths, settings, log`. The kernel never
imports a face. Tools import the kernel and never import each other, except `signal`,
which uses the canvas module.

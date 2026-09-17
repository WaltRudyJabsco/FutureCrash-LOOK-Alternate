# Changelog

## 4.0.0 — One kernel

A ground-up rewrite that keeps the principles of 3.x and replaces the machinery.
See `docs/WHY-4.md` for the reasoning and `docs/MIGRATING-FROM-3.md` for the details.

**Architecture**
- A package of focused modules replaces the 12k-line `lk` script. Python 3.9+, standard library only.
- LO and Future Crash's Oracle are personas of one kernel: shared tools, policy, journal, scheduler and lane, with separate temperaments and memories.
- One version number.

**Truth and safety**
- `Result` (`ok · dispatched · denied · refused · failed · cancelled`) replaces string-prefix receipts and English-matching guards.
- A host-computed ledger ends every turn.
- Tools a profile does not allow are never offered, and are refused again if named anyway.
- Writes outside the workspace ask: once, for the session, or always. Secret and system locations are unreachable in every profile.
- Unattended jobs deny instead of asking.

**Undo**
- One append-only journal covers LO, the filer, `lk cp/mv/rm/mk` and jobs.
- Reversals preflight against checksums and refuse when the world has changed.

**Mind**
- One structured-output reflection per exchange replaces four line-format passes.
- The explicit "remember this" fallback, decay, the 20/8/180 limits and summary rewriting are preserved.
- Skills keep Bundled + Learned. Bundled pack v2.

**Future Crash**
- The Signal Field is a tool with structured receipts. `[[SIGNAL]]` blocks are still read from tool-less models.
- The Signal canvas renders 1:1 when there is room, so drawn text stays legible.
- Oracle permission requests appear as an on-screen panel.
- Threads are the shared scheduler; results surface in the ambient view.
- Frames repaint only the rows that changed.

**LOOK**
- A declarative command registry drives dispatch, `lk commands`, `lk help all` and completion.
- The filer gains preview, find-under-home, hand-to-LO and cd-on-quit through a single handoff file.
- `eval "$(lk shell-init zsh|bash)"` replaces the 774-line zshrc and never shadows your names.
- `lk doctor`, `lk tools`, `lk log`, `lk benchmark` with a real tool-call probe, and `lk hosts discover` over Tailscale.

**Profile**
- `settings.json` replaces one-file-per-preference.
- `lk profile import-3x` is additive; export and restore are verified by manifest checksums, with a safety snapshot.

**Development**
- 53 tests against a scripted fake Ollama, passing on Python 3.9, 3.10 and 3.11.

For 3.x history see the 3.2.0 release.

# Coming from 3.x

## Automatic

`./install.sh` offers both of these. You can also run `lk profile import-3x [--dry-run]` at any time.

| 3.x | 4.0 |
|---|---|
| `~/.local/share/look/ollama_memory.json` | `profile/minds/lo/memory.json`: candidates and durable items become candidates, `core_summary` becomes the long-term summary |
| `lo_recent.json` | `profile/minds/lo/recent.json` |
| customised `core.md` | `profile/core.md` |
| `skills.md` Learned section | `profile/skills.md` Learned section |
| custom personality packs | `profile/personalities/` |
| `lo_access`, `lo_personality`, `lo_thinking`, `lo_think_display`, `ollama_model` | `settings.json` |
| `~/.config/look/feedback.json` | `settings.json` (`feedback.sound`, `feedback.motion`) |
| `ollama_hosts.json` | `~/.config/look/hosts.json` |
| `apps.json` | `~/.config/look/apps.json` |
| `OLLAMA_API_KEY` in `~/.zsh_secrets` | `~/.config/look/secrets.json` (0600); the environment variable still wins |
| `~/.future_crash/memory.json` summary | `profile/minds/future-crash/memory.json` |
| `# >>> LOOK Shell` block in `.zshrc` | replaced by one `eval "$(lk shell-init zsh)"` (backup kept) |

The import is additive. It never deletes or edits 3.x files.

## Not carried over

- **Undo history, receipts, events, queues.** These are runtime state. Undo what you need in 3.x before upgrading.
- **Filesystem grants.** 4.0 asks the first time, and "always" makes a grant permanent. `lk grants` lists them.
- **`schedule.json` and Future Crash threads.** Recreate them with `lk schedule add` or `T` in Future Crash.
- **ComfyUI, games, translation, tailnet Ollama sharing, services sharing, webterm, flight tracking.** These are candidates for extras (see `EXTENDING.md`); media control has already moved.

## Command map

| 3.x | 4.0 |
|---|---|
| `lk o …`, `lk ollama PROMPT` | `lo …` |
| `lk ollama models` / `lk models` | `lk models` |
| `lk ollama host …` | `lk hosts [use|add|forget|discover]` (the old spelling still works) |
| `lk ollama test`, `lk benchmark` | `lk benchmark [--all]` |
| `lk ollama key` | `lk web key` |
| `lk ollama access` | `lk access` |
| `lk receipts` | `lk journal` (alias kept) |
| `lk undo list · skip` | same |
| `lk ai status · start · stop · wake` | `lk ai status · start · stop` (wake is automatic) |
| `lk up`, `lk ports`, `lk port`, `lk processes`, `lk pid`, `lk git`, `lk run`, `lk env`, `lk path`, `lk why`, `lk disk`, `lk gpu`, `lk machine` | same |
| `lk tailscale`, `lk network`, `lk net` | `lk net` (aliases kept) |
| `lk config`, `lk settings` | `lk settings`, `lk set KEY VALUE` |
| `lk architecture-audit`, `lk tooling-audit`, `lk locator-test`, `lk command-audit` | `python3 -m unittest discover -s tests`, `lk tools`, `lk doctor` |
| `lk budget` | gone: budgets are structural now |
| `lcp lmv lrm lmk mkd` | `lk cp · mv · rm · mk` (shell shortcuts `lcp lmv lrm lmk` still defined) |
| `l ll ld lf lt lr lz ff` | same shortcuts, now generated |
| `mm mn mp` | same (media extra) |
| `zll`, `cdl`, `fznv` | not included; `l WORD` does the jump, and `E` in the filer opens `$EDITOR` |

## Behaviour you will notice

- Every LO turn ends with a ledger line. Set `lk set lo.ledger actions` to see it only when tools ran.
- LO no longer rewrites your path words. Writing outside the folder you started in asks first.
- The Oracle's default authority is **conservative**. Raise it with `lk set crash.authority workspace`.
- Upper-case letters are commands in the filer. Lower-case letters, digits and `. - _ ~ /` filter.

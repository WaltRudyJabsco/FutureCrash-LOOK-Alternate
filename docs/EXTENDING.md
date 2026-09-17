# Extending LOOK

## A tool

```python
from look.kernel.results import Result
from look.kernel.tools import READ, Context, Param, registry
from look.kernel.policy import check_path, resolve


@registry.tool("count_words", "Count words in a text file.", READ,
               params={"path": Param("string", "File to count")})
def count_words(ctx: Context, path: str) -> Result:
    target = resolve(ctx, path)
    stop = check_path(ctx, target, "read")      # the boundary: workspace, grants, secrets
    if stop:
        return stop
    n = len(target.read_text(errors="replace").split())
    return Result.done(f"{target.name}: {n} words", words=n)
```

Rules that keep the system honest:

- **Declare the capability you actually need.** The profile table decides who is offered the tool.
- **Return a `Result`, never prose.** Write the receipt as what the host observed.
- **Changes go through `kernel/fsops.py`** (or return an `undo` recipe) so `lk undo` works.
- **Handing something to another program is `Result.dispatched`**, not `done`.
- **Use `available=`** for tools that need a key or a binary.
- **Use `personas=`** for tools that only make sense in one face.

## An extra

Put a module in `look/extras/` (it ships with LOOK) or in
`~/.local/share/look/profile/extras/` (it is yours, and it travels with your profile):

```python
# ~/.local/share/look/profile/extras/pomodoro.py
import time


def register(commands, tools):
    @commands.command("pomodoro", "[MINUTES]", "a focus timer", "extra")
    def pomodoro(args):
        minutes = int(args[0]) if args else 25
        time.sleep(minutes * 60)
        print("\a time")
        return 0
```

`tools` is the same registry as above, so an extra can give LO new abilities too. A
broken extra is logged and skipped; it never stops LOOK from starting.

## A personality

Drop a Markdown file in `~/.local/share/look/profile/personalities/`, then run
`lk personality NAME`. End it the way the bundled ones do: personality never changes
factual standards, safety, permissions or tool discipline.

## Tests

`tests/fake_ollama.py` is a scripted Ollama. A test gives it steps (`{"tool_calls": [call("write_file", …)]}`,
`{"content": "…"}`, or a callable that inspects the request) and asserts on files, the
journal, the ledger and the requests the model received.

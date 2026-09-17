"""Shell commands: power (each confirmed) and unsafe (unconfirmed) only."""
from __future__ import annotations

import os
import subprocess

from ..kernel.policy import confirm_command
from ..kernel.results import Result
from ..kernel.tools import EXECUTE, EXEC, Context, Param, registry

OUTPUT_LIMIT = 20_000


@registry.tool("run_command", "Run a shell command in the workspace. Output is captured. Prefer specific tools when one fits.",
               EXEC, EXECUTE, params={"command": Param("string", "The command line"),
                                     "timeout": Param("integer", "Seconds (default 60, max 600)", required=False)})
def run_command(ctx: Context, command: str, timeout: int = 60) -> Result:
    command = str(command).strip()
    if not command:
        return Result.refused("empty command")
    if not confirm_command(ctx, command):
        return Result.denied(f"you declined: $ {command}")
    timeout = max(1, min(600, int(timeout or 60)))
    try:
        proc = subprocess.run(command, shell=True, cwd=str(ctx.workspace), capture_output=True, text=True,
                              timeout=timeout, env=dict(os.environ, LOOK_AGENT="1"))
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return Result.failed(f"$ {command} timed out after {timeout}s", command=command, output=out[-OUTPUT_LIMIT:])
    output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT // 2] + "\n… [truncated] …\n" + output[-OUTPUT_LIMIT // 2:]
    receipt = f"$ {command[:100]} → exit {proc.returncode}"
    # A command ran either way; exit status is data, not a policy failure.
    result = Result.done(receipt, output, exit_code=proc.returncode) if proc.returncode == 0 else \
        Result.failed(receipt, exit_code=proc.returncode)
    result.content = output
    # Commands are not journal-reversible; say so honestly by leaving undo empty.
    return result

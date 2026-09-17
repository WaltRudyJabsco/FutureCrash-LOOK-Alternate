"""The Signal canvas as a tool (Future Crash's Oracle only)."""
from __future__ import annotations

from ..kernel.results import Result
from ..kernel.tools import DRAW, Context, Param, registry
from ..faces.crash.canvas import LANGUAGE, Canvas, receipt_line


@registry.tool("signal", "Draw on the 40x12 Signal Field. One command per line:\n" + LANGUAGE, DRAW, label="signal",
               params={"program": Param("string", "Drawing program, one command per line")},
               personas=["future-crash"])
def signal(ctx: Context, program: str) -> Result:
    canvas = Canvas()
    canvas.load()
    receipt = canvas.run(program)
    canvas.save()
    if not receipt["accepted"]:
        return Result.refused(receipt_line(receipt) + " · nothing drawn; check the command syntax", **receipt)
    return Result.done(receipt_line(receipt), **receipt)

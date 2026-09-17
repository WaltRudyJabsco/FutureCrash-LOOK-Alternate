"""One diagnostic log instead of silently swallowed exceptions.

The rule in 4.0: catch broadly only at boundaries (a tool run, a UI frame, a
daemon job) and, when you do, say so here.  `lk log` shows the tail.
"""
from __future__ import annotations

import os
import sys
import time
import traceback


class _Log:
    def __init__(self):
        self.echo = bool(os.environ.get("LOOK_DEBUG"))

    def _path(self):
        from .paths import roots
        return roots.log

    def _write(self, level: str, where: str, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:5} {where}: {message}\n"
        if self.echo:
            sys.stderr.write(line)
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 1_000_000:
                os.replace(path, path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # logging must never become the failure

    def info(self, where: str, message: str) -> None:
        self._write("info", where, message)

    def warn(self, where: str, message: str) -> None:
        self._write("warn", where, message)

    def error(self, where: str, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            last = tb[-1]
            detail += f" [{os.path.basename(last.filename)}:{last.lineno}]"
        self._write("error", where, detail)


log = _Log()

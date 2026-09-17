"""Entry point.  python -m look [lk|lo|future-crash|looked] ...

The installer creates tiny launchers (lk, lo, future-crash) that call this with
the face name first; with no face name, `lk` is assumed.
"""
from __future__ import annotations

import os
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 9):
        print("LOOK needs Python 3.9 or newer.", file=sys.stderr)
        return 1
    try:
        os.getcwd()
    except FileNotFoundError:  # the directory you were in was deleted underneath the shell
        os.chdir(os.path.expanduser("~"))
        print("(current directory no longer exists; using your home folder)", file=sys.stderr)
    face = "lk"
    if argv and argv[0] in ("lk", "lo", "future-crash", "fcr", "rst", "looked"):
        face = argv.pop(0)
    try:
        if face == "lo":
            from .faces import lo
            return lo.main(argv)
        if face in ("future-crash", "fcr", "rst"):
            from .faces.crash import app
            return app.main(argv)
        if face == "looked":
            from .daemon import looked
            return looked.run()
        from .faces import cli
        return cli.main(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # boundary: last resort, with a pointer to the log
        from .log import log
        log.error(f"main.{face}", exc)
        if os.environ.get("LOOK_DEBUG"):
            raise
        print(f"{face}: {type(exc).__name__}: {exc}  (details: lk log · rerun with LOOK_DEBUG=1)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

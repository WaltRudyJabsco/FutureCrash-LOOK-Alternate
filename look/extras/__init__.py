"""Extras: optional features that register commands and tools without touching the core.

An extra is a module in this package (or in profile/extras/*.py) exposing
    register(commands, tools)
where `commands` is the lk command registry and `tools` the tool registry.
The core never imports an extra by name.
"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil

from ..log import log
from ..paths import roots

_loaded = False


def load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from ..faces.commands import commands
    from ..kernel.tools import registry
    modules = []
    for info in pkgutil.iter_modules(__path__):
        try:
            modules.append(importlib.import_module(f"{__name__}.{info.name}"))
        except Exception as exc:  # boundary: a broken extra must not break LOOK
            log.error(f"extras.{info.name}", exc)
    user_dir = roots.profile / "extras"
    for path in sorted(user_dir.glob("*.py")) if user_dir.is_dir() else []:
        try:
            spec = importlib.util.spec_from_file_location(f"look_user_extra_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            modules.append(mod)
        except Exception as exc:
            log.error(f"extras.user.{path.stem}", exc)
    for mod in modules:
        fn = getattr(mod, "register", None)
        if callable(fn):
            try:
                fn(commands, registry)
            except Exception as exc:
                log.error(f"extras.register.{mod.__name__}", exc)

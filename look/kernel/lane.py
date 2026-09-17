"""The shared inference lane.

Many clients, one GPU. Interactive work (a person waiting) holds a lease;
background work (memory reflection, scheduled jobs, Future Crash's ambient
musings) only starts when nobody holds one.

Leases are tiny files with an expiry, so the lane works identically whether or
not the `looked` daemon is running — there is no "fallback mode".

    with lane.interactive("lo"):        # LO is answering someone
        ...
    if lane.idle(): ...                 # safe to start background inference
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from typing import Iterator, List

from ..paths import roots

LEASE_SECONDS = 20.0
PRIORITY = {"interactive": 0, "user-background": 1, "maintenance": 2, "ambient": 3}


def _lease_path(client: str):
    return roots.leases / f"{client}-{os.getpid()}.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def leases() -> List[dict]:
    out = []
    now = time.time()
    try:
        entries = list(roots.leases.iterdir())
    except FileNotFoundError:
        return out
    for p in entries:
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if data.get("expires", 0) < now or not _pid_alive(int(data.get("pid", 0))):
            try:
                p.unlink()
            except OSError:
                pass
            continue
        out.append(data)
    return out


def idle(ignore_pid: bool = True) -> bool:
    """True when no *other* process holds an interactive lease."""
    me = os.getpid()
    return not any(l for l in leases() if not (ignore_pid and l.get("pid") == me))


def busy_clients() -> List[str]:
    return sorted({str(l.get("client")) for l in leases()})


@contextlib.contextmanager
def interactive(client: str) -> Iterator[None]:
    roots.leases.mkdir(parents=True, exist_ok=True)
    path = _lease_path(client)
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            try:
                path.write_text(json.dumps({"client": client, "pid": os.getpid(),
                                            "expires": time.time() + LEASE_SECONDS}))
            except OSError:
                pass
            stop.wait(LEASE_SECONDS / 3)

    thread = threading.Thread(target=beat, name=f"lease-{client}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
        try:
            path.unlink()
        except OSError:
            pass
        _poke_daemon()


def _poke_daemon() -> None:
    """Tell looked the lane may be free. Silent when no daemon is running."""
    try:
        from ..daemon.client import send
        send({"op": "wake"}, timeout=0.2)
    except Exception:
        pass

"""looked — the optional resident coordinator.

It owns exactly three things: firing schedules, draining the job queue when the
lane is idle, and answering status. It does not own memory, tools, rendering or
policy — those are library code any process runs identically. If looked is not
running, queued work is drained by a short-lived process instead; nothing
behaves differently.

It imports the package, never a script, so an upgrade only needs a restart.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from typing import Optional

from .. import __version__, store
from ..log import log
from ..paths import roots
from . import jobs, schedule

TICK = 5.0


class Looked:
    def __init__(self):
        self.running = True
        self.wake = threading.Event()
        self.started = time.time()
        self.current = "idle"
        self.completed = 0

    # ---- socket ----------------------------------------------------------
    def serve(self) -> None:
        roots.ensure()
        if roots.socket.exists():
            from .client import send
            if send({"op": "ping"}, timeout=0.5):
                raise SystemExit("looked is already running")
            roots.socket.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(roots.socket))
        os.chmod(roots.socket, 0o600)
        srv.listen(8)
        srv.settimeout(1.0)
        store.write_text(roots.pidfile, str(os.getpid()))
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        worker = threading.Thread(target=self.work, name="looked-work", daemon=True)
        worker.start()
        log.info("looked", f"started pid {os.getpid()}")
        try:
            while self.running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            for p in (roots.socket, roots.pidfile):
                try:
                    p.unlink()
                except OSError:
                    pass
            log.info("looked", "stopped")

    def handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(2.0)
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                msg = json.loads(raw.decode() or "{}")
                reply = self.dispatch(msg)
            except (OSError, ValueError) as exc:
                reply = {"ok": False, "error": str(exc)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass

    def dispatch(self, msg: dict) -> dict:
        op = msg.get("op")
        if op == "ping":
            return {"ok": True}
        if op == "wake":
            self.wake.set()
            return {"ok": True}
        if op == "stop":
            self.stop()
            return {"ok": True}
        if op == "status":
            from ..kernel import lane
            return {"ok": True, "pid": os.getpid(), "version": __version__, "uptime": time.time() - self.started,
                    "current": self.current, "completed": self.completed, "queue": jobs.counts(),
                    "leases": lane.busy_clients(), "scheduled": len([e for e in schedule.load() if e.get("enabled", True)])}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def stop(self) -> None:
        self.running = False
        self.wake.set()

    # ---- work ------------------------------------------------------------
    def work(self) -> None:
        while self.running:
            try:
                schedule.dispatch_due()
                self.current = "draining"
                self.completed += jobs.drain(stop=lambda: not self.running)
            except Exception as exc:  # boundary: the daemon keeps living
                log.error("looked.work", exc)
            self.current = "idle"
            self.wake.wait(TICK)
            self.wake.clear()


def run() -> int:
    Looked().serve()
    return 0


def pid() -> Optional[int]:
    try:
        value = int(store.read_text(roots.pidfile).strip() or 0)
        os.kill(value, 0)
        return value
    except (ValueError, OSError):
        return None

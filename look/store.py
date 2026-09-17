"""Small, boring persistence primitives.

Every durable write in LOOK goes through here: atomic JSON replace, bounded
append-only JSON lines, and an advisory file lock. Nothing else in the codebase
opens state files for writing directly.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .log import log


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warn("store.read_json", f"{path}: {exc}")
        return default


def write_json(path: Path, data: Any, private: bool = True) -> None:
    """Write-temp-then-rename so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        log.warn("store.read_text", f"{path}: {exc}")
        return default


def write_text(path: Path, text: str, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class JsonLines:
    """Append-only log with size-bounded rotation (current + N archives)."""

    def __init__(self, path: Path, max_bytes: int = 2_000_000, archives: int = 5):
        self.path = path
        self.max_bytes = max_bytes
        self.archives = archives

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with locked(self.path.with_suffix(".lock")):
            self._rotate_if_needed(len(line))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self.max_bytes:
            return
        for n in range(self.archives - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{n}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{n + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def read(self, limit: Optional[int] = None, include_archives: bool = False) -> List[dict]:
        files = [self.path]
        if include_archives:
            files = [self.path.with_name(f"{self.path.name}.{n}") for n in range(self.archives, 0, -1)] + files
        rows: List[dict] = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rows.append(json.loads(raw))
                        except ValueError:
                            # A torn final line from a crash is skipped, never fatal.
                            continue
            except FileNotFoundError:
                continue
        return rows[-limit:] if limit else rows


@contextlib.contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"lock busy: {path}")
                time.sleep(0.02)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def try_lock(path: Path):
    """Non-blocking exclusive lock held for the life of the returned handle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        fh.close()
        return None

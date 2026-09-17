"""Talk to looked over its Unix socket. Returns None when it is not running."""
from __future__ import annotations

import json
import socket
from typing import Optional

from ..paths import roots


def send(message: dict, timeout: float = 2.0) -> Optional[dict]:
    path = roots.socket
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(message) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode() or "null")
    except (OSError, ValueError):
        return None

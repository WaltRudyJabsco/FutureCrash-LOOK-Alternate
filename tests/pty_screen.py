"""Drive a TUI in a pseudo-terminal and read the rendered screen (dev-only; needs `pyte`)."""
from __future__ import annotations

import os
import pty
import select
import signal
import struct
import sys
import time
import fcntl
import termios

import pyte


class Screen:
    def __init__(self, argv, cols=120, rows=34, env=None, cwd=None):
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        pid, fd = pty.fork()
        if pid == 0:
            if cwd:
                os.chdir(cwd)
            os.environ.update(env or {})
            os.environ["TERM"] = "xterm-256color"
            os.execvp(argv[0], argv)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(pid, signal.SIGWINCH)

    def pump(self, seconds=0.5):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.stream.feed(data)

    def send(self, keys: str, wait=0.4):
        os.write(self.fd, keys.encode())
        self.pump(wait)

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass

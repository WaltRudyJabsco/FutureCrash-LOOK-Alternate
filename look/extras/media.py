"""Extra: media transport for an already-open player.

    lk media [toggle|next|prev|stop]      (shell shortcuts: mm mn mp)

macOS drives Music or Spotify through AppleScript; Linux uses MPRIS via playerctl.
Every action reports the player state it can observe afterwards, never a guess.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import time

VERBS = {"toggle": ("playpause", "play-pause"), "next": ("next track", "next"),
         "prev": ("previous track", "previous"), "stop": ("pause", "stop")}


def _osa(script: str) -> str:
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=4).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _mac_app() -> str:
    for app in ("Spotify", "Music"):
        if _osa(f'application "{app}" is running') == "true":
            return app
    return ""


def status() -> str:
    if platform.system() == "Darwin":
        app = _mac_app()
        if not app:
            return "no player running (Music or Spotify)"
        state = _osa(f'tell application "{app}" to player state as string')
        track = _osa(f'tell application "{app}" to (name of current track) & " — " & (artist of current track)')
        return f"{app} · {state} · {track}"
    if shutil.which("playerctl"):
        try:
            state = subprocess.run(["playerctl", "status"], capture_output=True, text=True, timeout=3).stdout.strip()
            meta = subprocess.run(["playerctl", "metadata", "--format", "{{title}} — {{artist}}"],
                                  capture_output=True, text=True, timeout=3).stdout.strip()
            return f"{state or 'no player'} · {meta}" if state else "no MPRIS player"
        except (OSError, subprocess.SubprocessError):
            return "playerctl failed"
    return "install playerctl for media control on Linux"


def act(verb: str) -> str:
    mac, linux = VERBS[verb]
    if platform.system() == "Darwin":
        app = _mac_app()
        if not app:
            return "no player running (Music or Spotify)"
        _osa(f'tell application "{app}" to {mac}')
    elif shutil.which("playerctl"):
        subprocess.run(["playerctl", linux], capture_output=True, timeout=3)
    time.sleep(0.25)
    return status()


def register(commands, tools) -> None:
    @commands.command("media", "[toggle|next|prev|stop]", "control the music already playing", "extra",
                      aliases=["music"], complete=lambda: list(VERBS))
    def cmd_media(args):
        verb = args[0] if args else None
        if verb and verb not in VERBS:
            print("usage: lk media [toggle|next|prev|stop]")
            return 2
        print(act(verb) if verb else status())
        return 0

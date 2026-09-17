"""Semantic feedback: callers name an event, this module decides presentation.

Events: accept · complete · notify · error · oracle · fortune · panic · recover
Sound is off by default and generated locally (tiny cached WAVs). Nothing
plays, and nothing animates, when output is not a terminal.
"""
from __future__ import annotations

import math
import platform
import shutil
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

from ..paths import roots
from ..settings import settings

TONES = {
    "accept": [(660, 0.05)],
    "complete": [(523, 0.06), (784, 0.09)],
    "notify": [(880, 0.07), (880, 0.07)],
    "error": [(220, 0.14)],
    "oracle": [(392, 0.05), (587, 0.05), (784, 0.08)],
    "fortune": [(740, 0.04), (988, 0.06)],
    "panic": [(180, 0.08), (140, 0.12)],
    "recover": [(440, 0.05), (660, 0.07)],
    "ask": [(600, 0.04)],
}


def _wav(event: str) -> Path:
    path = roots.cache / "tones" / f"{event}.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 22050
    frames = bytearray()
    for freq, dur in TONES[event]:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 400)
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def sound_enabled() -> bool:
    return bool(settings.get("feedback.sound")) and sys.stdout.isatty()


def play(event: str, force: bool = False) -> None:
    if event not in TONES or not (force or sound_enabled()):
        return
    player = "afplay" if platform.system() == "Darwin" else next((p for p in ("paplay", "aplay") if shutil.which(p)), None)
    if not player or not shutil.which(player):
        return

    def go():
        try:
            args = [player, str(_wav(event))]
            if player == "aplay":
                args.insert(1, "-q")
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass

    threading.Thread(target=go, daemon=True).start()


def motion() -> str:
    return settings.get("feedback.motion") if sys.stdout.isatty() else "off"


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

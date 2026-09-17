"""Every test runs in its own throwaway HOME and LOOK_HOME."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


class LookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="look-test-")).resolve()
        self.home = self.tmp / "home"
        self.ws = self.home / "work"
        self.ws.mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in ("HOME", "LOOK_HOME", "LOOK_NO_SPAWN", "OLLAMA_HOST", "OLLAMA_API_KEY",
                                                    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "LOOK_MODEL", "LOOK_ACCESS")}
        for k in self._env:
            os.environ.pop(k, None)
        os.environ.update({"HOME": str(self.home), "LOOK_HOME": str(self.tmp / "look"), "LOOK_NO_SPAWN": "1"})
        from look.settings import settings
        settings._cache = None
        from look.mind import personas
        personas.ensure_profile()

    def tearDown(self):
        os.chdir(ROOT)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, access="workspace", interactive=False, answer="n"):
        from look.kernel.tools import Context

        class UI:
            def __init__(self):
                self.asked = []

            def ask(self, prompt, choices, default):
                self.asked.append(prompt)
                return answer

        return Context(workspace=self.ws, access=access, interactive=interactive, ui=UI())

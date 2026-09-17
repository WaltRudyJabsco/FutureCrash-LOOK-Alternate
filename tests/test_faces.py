from base import LookTest

import io
import json
import shutil
import subprocess
import time
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from fake_ollama import FakeOllama


class CanvasTests(LookTest):
    def test_program_receipt_and_clipping(self):
        from look.faces.crash.canvas import Canvas
        c = Canvas()
        r = c.run("CLEAR\nTITLE T\nBOX 0 0 40 12 cyan #\nPUT 99 99 red x\nNONSENSE 1 2")
        self.assertEqual((r["accepted"], r["rejected"], r["clipped"]), (4, 1, 1))  # CLEAR TITLE BOX PUT(clipped) · NONSENSE
        self.assertTrue(c.active())

    def test_frames_and_sprites(self):
        from look.faces.crash.canvas import Canvas
        c = Canvas()
        r = c.run("TITLE anim\nFRAME\nPUT 1 1 green a\nFRAME\nSPRITE 2 2 amber\n/\\\n\\/\nEND")
        self.assertEqual(r["frames"], 2)

    def test_sampling_keeps_text_legible(self):
        from look.faces.crash.canvas import Canvas
        c = Canvas()
        c.run("TEXT 0 0 white HELLO")
        rows = c.sample(60, 14)
        text = "".join(cell[0] if cell else " " for cell in rows[1])
        self.assertIn("HELLO", text)

    def test_signal_tool_persists_for_the_face(self):
        from look.faces.crash.canvas import Canvas
        from look.kernel.tools import load_builtin_tools, invoke
        tool = load_builtin_tools().get("signal")
        r, _ = invoke(tool, self.ctx("conservative"), {"program": "CIRCLE 20 6 3 cyan o"})
        self.assertTrue(r.ok)
        face = Canvas()
        self.assertTrue(face.load())
        self.assertTrue(face.active())

    def test_text_block_fallback(self):
        from look.faces.crash.canvas import extract_blocks
        clean, blocks = extract_blocks("Look.\n[[SIGNAL]]\nPUT 1 1 red x\n[[/SIGNAL]]")
        self.assertEqual(clean, "Look.")
        self.assertEqual(len(blocks), 1)


class ProfileTests(LookTest):
    def test_export_restore_roundtrip(self):
        from look import profile
        from look.mind.memory import Mind
        m = Mind("lo")
        d = m.load()
        m.add(d, "loves Kurosawa films", 70)
        m.save(d)
        archive = profile.export(str(self.tmp / "p.zip"))
        m.clear()
        count, safety = profile.restore(str(archive))
        self.assertGreater(count, 3)
        self.assertIn("Kurosawa", json.dumps(Mind("lo").load()))
        self.assertTrue(safety.exists())

    def test_restore_rejects_tampering_and_traversal(self):
        from look import profile
        archive = profile.export(str(self.tmp / "p.zip"))
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(bad, "w") as dst:
            for item in src.infolist():
                data = src.read(item)
                if item.filename.endswith("core.md"):
                    data = b"tampered"
                dst.writestr(item, data)
        with self.assertRaises(ValueError):
            profile.restore(str(bad))
        evil = self.tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../escape.txt", "x")
        with self.assertRaises(ValueError):
            profile.restore(str(evil))

    def test_import_3x(self):
        from look import profile
        from look.kernel.ollama import load_hosts
        from look.mind import skills
        from look.mind.memory import Mind
        from look.settings import secret, settings
        legacy = self.home / ".local" / "share" / "look"
        legacy.mkdir(parents=True)
        (legacy / "ollama_memory.json").write_text(json.dumps({
            "schema_version": 1, "core_summary": "Writer and novelist.",
            "memories": [{"text": "Working on American Chrome", "importance": 60}],
            "durable": [{"text": "Prefers concise answers", "importance": 90}]}))
        (legacy / "lo_access").write_text("power\n")
        (legacy / "ollama_model").write_text("gemma3:12b")
        (legacy / "skills.md").write_text("# LO Skills\n\n## Bundled\n\n- x\n\n## Learned\n\n- Batch similar file operations together.\n")
        (legacy / "ollama_hosts.json").write_text(json.dumps({"default": "desk", "hosts": {"desk": {"url": "http://100.1.2.3:11434"}}}))
        (self.home / ".zsh_secrets").write_text("export OLLAMA_API_KEY='abc123'\n")
        notes = profile.import_3x()
        self.assertTrue(notes)
        self.assertEqual(settings.get("lo.access"), "power")
        self.assertEqual(settings.get("ai.model"), "gemma3:12b")
        data = Mind("lo").load()
        self.assertEqual(data["summary"], "Writer and novelist.")
        self.assertEqual(len(data["candidates"]), 2)
        self.assertIn("Batch similar file operations together.", skills.load()[1])
        self.assertEqual(load_hosts()["default"], "desk")
        self.assertEqual(secret("OLLAMA_API_KEY"), "abc123")
        self.assertTrue((legacy / "ollama_memory.json").exists(), "import never deletes legacy state")


class ScheduleJobTests(LookTest):
    def test_intervals(self):
        from look.daemon import schedule
        self.assertEqual(schedule.parse_every("2h"), 7200)
        self.assertIsNone(schedule.parse_every("soon"))
        with self.assertRaises(ValueError):
            schedule.add("x", every=10)

    def test_due_entries_become_jobs_and_advance(self):
        from look.daemon import jobs, schedule
        e = schedule.add("check the news", every=3600)
        entries = schedule.load()
        entries[0]["next"] = time.time() - 7300
        schedule.save(entries)
        self.assertEqual(schedule.dispatch_due(), 1)
        self.assertEqual(jobs.counts()["job"], 1)
        self.assertGreater(schedule.load()[0]["next"], time.time())
        self.assertEqual(schedule.dispatch_due(), 0, "missed runs collapse into one")

    def test_drain_runs_reflection(self):
        import os
        from look.daemon import jobs
        from look.kernel.journal import journal
        with FakeOllama(json_replies=[json.dumps({"memories": [{"text": "Shoots 35mm film photography", "importance": 70}],
                                                   "skill": "", "durable": False})]) as fake:
            jobs.enqueue("reflect", {"mind": "lo", "persona": "lo", "user": "I shoot 35mm film", "assistant": "Nice.",
                                     "model": "fake:1b", "host": fake.url}, spawn=False)
            self.assertEqual(jobs.drain(), 1)
        self.assertEqual(jobs.counts()["reflect"], 0)
        self.assertTrue(any(e["name"] == "reflect.done" for e in journal.events()))

    def test_unreachable_model_keeps_job(self):
        from look.daemon import jobs
        jobs._backoff_until = 0
        jobs.enqueue("reflect", {"mind": "lo", "user": "x", "assistant": "y", "model": "m", "host": "http://127.0.0.1:9"}, spawn=False)
        self.assertEqual(jobs.drain(), 0)
        self.assertEqual(jobs.counts()["reflect"], 1)
        jobs._backoff_until = 0


class CliTests(LookTest):
    def run_lk(self, *argv):
        from look.faces import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_registry_longest_prefix_and_aliases(self):
        from look.faces import cli  # noqa: F401  (registers commands)
        from look.faces.commands import commands
        cmd, rest = commands.resolve(["ollama", "host", "use", "desk"])
        self.assertEqual(cmd.name, "hosts")
        self.assertEqual(rest, ["use", "desk"])
        self.assertIn("memory", commands.words())

    def test_file_commands_are_journaled(self):
        (self.ws / "a.txt").write_text("a")
        import os
        os.chdir(self.ws)
        code, out = self.run_lk("mv", "a.txt", "b.txt")
        self.assertEqual(code, 0, out)
        code, out = self.run_lk("undo")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.ws / "a.txt").exists())

    def test_every_command_has_docs(self):
        from look.faces import cli  # noqa: F401
        from look.faces.commands import AREAS, commands
        areas = {a for a, _ in AREAS}
        for c in commands.all(include_hidden=True):
            self.assertTrue(c.summary, c.name)
            self.assertIn(c.area, areas, c.name)

    def test_shell_init_is_valid_shell(self):
        _, out = self.run_lk("shell-init", "bash")
        for shell in ("bash", "zsh"):
            if not shutil.which(shell):
                continue
            _, script = self.run_lk("shell-init", shell)
            proc = subprocess.run([shell, "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

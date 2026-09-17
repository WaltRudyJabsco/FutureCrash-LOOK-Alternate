from base import LookTest

import os
from pathlib import Path


class PolicyTests(LookTest):
    def test_profiles_are_monotonic(self):
        from look.kernel.policy import PROFILES
        order = ["conservative", "workspace", "power", "unsafe"]
        for a, b in zip(order, order[1:]):
            self.assertTrue(PROFILES[a] <= PROFILES[b], f"{a} ⊄ {b}")

    def test_conservative_cannot_write_even_if_model_asks(self):
        from look.kernel.tools import invoke, load_builtin_tools
        reg = load_builtin_tools()
        r, _ = invoke(reg.get("write_file"), self.ctx("conservative"), {"path": "a.txt", "content": "x"})
        self.assertEqual(r.status, "denied")
        self.assertFalse((self.ws / "a.txt").exists())

    def test_outside_workspace_write_needs_a_person(self):
        from look.kernel.tools import invoke, load_builtin_tools
        reg = load_builtin_tools()
        outside = self.home / "Downloads" / "x.txt"
        r, _ = invoke(reg.get("write_file"), self.ctx("workspace", interactive=False), {"path": str(outside), "content": "x"})
        self.assertEqual(r.status, "denied")
        self.assertTrue(r.data.get("needs_grant"))
        ctx = self.ctx("workspace", interactive=True, answer="s")
        r, _ = invoke(reg.get("write_file"), ctx, {"path": str(outside), "content": "x"})
        self.assertTrue(r.ok, r.receipt)
        r2, _ = invoke(reg.get("write_file"), ctx, {"path": str(outside.parent / "y.txt"), "content": "y"})
        self.assertTrue(r2.ok)
        self.assertEqual(len(ctx.ui.asked), 1, "a session grant should not ask twice")

    def test_always_grant_persists(self):
        from look.kernel.policy import check_path, list_grants
        target = self.home / "Music" / "a.txt"
        self.assertIsNone(check_path(self.ctx("workspace", interactive=True, answer="a"), target, "write"))
        self.assertEqual(len(list_grants()), 1)
        self.assertIsNone(check_path(self.ctx("workspace", interactive=False), target, "write"))

    def test_secret_locations_are_never_reachable(self):
        from look.kernel.policy import check_path
        (self.home / ".ssh").mkdir()
        for access in ("workspace", "unsafe"):
            r = check_path(self.ctx(access, interactive=True, answer="a"), self.home / ".ssh" / "id_ed25519", "read")
            self.assertEqual(r.status, "denied")

    def test_system_writes_refused(self):
        from look.kernel.policy import check_path
        r = check_path(self.ctx("power", interactive=True, answer="o"), Path("/etc/hosts"), "write")
        self.assertEqual(r.status, "denied")

    def test_profile_is_not_editable_by_tools(self):
        from look.kernel.policy import check_path
        from look.paths import roots
        r = check_path(self.ctx("unsafe", interactive=True, answer="o"), roots.skills, "write")
        self.assertEqual(r.status, "denied")


class FsopsUndoTests(LookTest):
    def undo(self):
        from look.kernel.journal import journal
        return journal.undo()

    def record(self, result):
        from look.kernel.journal import journal

        class T:
            name, effect = "test", "change"
        journal.record_tool("test", T, {}, result)
        return result

    def test_copy_never_overwrites(self):
        from look.kernel import fsops
        (self.ws / "a.txt").write_text("1")
        r = self.record(fsops.copy_paths([self.ws / "a.txt"], self.ws, str(self.ws) + "/"))
        self.assertTrue((self.ws / "a copy.txt").exists(), r.receipt)
        self.assertTrue(self.undo().ok)
        self.assertFalse((self.ws / "a copy.txt").exists())

    def test_move_refuses_clobber_and_undo_moves_back(self):
        from look.kernel import fsops
        (self.ws / "a.txt").write_text("1")
        (self.ws / "b.txt").write_text("2")
        self.assertEqual(fsops.move_paths([self.ws / "a.txt"], self.ws / "b.txt").status, "refused")
        self.record(fsops.move_paths([self.ws / "a.txt"], self.ws / "sub" / "c.txt"))
        self.assertEqual((self.ws / "sub" / "c.txt").read_text(), "1")
        self.assertTrue(self.undo().ok)
        self.assertEqual((self.ws / "a.txt").read_text(), "1")
        self.assertFalse((self.ws / "sub").exists(), "created parent dirs are reversed too")

    def test_remove_goes_to_trash(self):
        from look.kernel import fsops
        d = self.ws / "d"
        d.mkdir()
        (d / "f").write_text("keep me")
        self.record(fsops.remove_paths([d]))
        self.assertFalse(d.exists())
        self.assertTrue(self.undo().ok)
        self.assertEqual((d / "f").read_text(), "keep me")

    def test_overwrite_keeps_backup_and_refuses_if_changed_since(self):
        from look.kernel import fsops
        f = self.ws / "notes.md"
        f.write_text("original")
        self.assertEqual(fsops.write_file(f, "new").status, "refused")
        self.record(fsops.write_file(f, "rewritten", "overwrite"))
        self.assertTrue(self.undo().ok)
        self.assertEqual(f.read_text(), "original")
        self.record(fsops.write_file(f, "second", "overwrite"))
        f.write_text("the user edited this afterwards")
        r = self.undo()
        self.assertEqual(r.status, "refused")
        self.assertEqual(f.read_text(), "the user edited this afterwards")

    def test_create_files_is_one_reversible_change(self):
        from look.kernel import fsops
        r = self.record(fsops.create_files([{"path": "a/1.txt", "content": "1"}, {"path": "a/b/2.txt", "content": "2"}],
                                           lambda raw: self.ws / raw))
        self.assertEqual(r.changes, 2)
        self.assertTrue(self.undo().ok)
        self.assertEqual(list(self.ws.iterdir()), [])

    def test_undo_stack_states(self):
        from look.kernel import fsops
        from look.kernel.journal import journal
        self.record(fsops.touch(self.ws / "one"))
        self.record(fsops.touch(self.ws / "two"))
        journal.skip()
        states = [s for _, s in journal.undo_stack()]
        self.assertEqual(states, ["skipped", "ready"])
        self.assertTrue(journal.undo().ok)
        self.assertFalse((self.ws / "one").exists())
        self.assertTrue((self.ws / "two").exists())


class ResultTests(LookTest):
    def test_statuses_are_closed(self):
        from look.kernel.results import Result
        with self.assertRaises(ValueError):
            Result(True, "playing", "VLC is playing")

    def test_dispatch_is_not_success_prose(self):
        from look.tools.host import resource_kind
        self.assertEqual(resource_kind("https://www.youtube.com/watch?v=abc"), "video")
        self.assertEqual(resource_kind("https://youtube.com/@channel"), "channel")

    def test_open_url_play_requires_video(self):
        from look.tools.host import open_url
        r = open_url(self.ctx(), "https://www.youtube.com/@somebody", intent="play")
        self.assertEqual(r.status, "refused")


class SettingsTests(LookTest):
    def test_coercion_and_env(self):
        from look.settings import settings
        settings.set("feedback.sound", "on")
        self.assertIs(settings.get("feedback.sound"), True)
        with self.assertRaises(ValueError):
            settings.set("lo.access", "root")
        os.environ["LOOK_ACCESS"] = "conservative"
        self.assertEqual(settings.get("lo.access"), "conservative")

from base import LookTest

from fake_ollama import FakeOllama, call


class AgentTests(LookTest):
    def agent(self, fake, access="workspace", **kw):
        from look.kernel.agent import Agent, UI
        from look.kernel.ollama import Ollama
        from look.mind import personas

        class Recorder(UI):
            def __init__(self):
                self.receipts = []

            def receipt(self, tool, result, elapsed):
                self.receipts.append(result)

        return Agent(personas.LO, client=Ollama(fake.url), model="fake:1b", access=access, workspace=self.ws,
                     ui=Recorder(), interactive=False, **kw)

    def test_tool_round_then_answer_and_ledger(self):
        script = [{"tool_calls": [call("create_files", files=[{"path": "a.md", "content": "a"}, {"path": "b.md", "content": "b"}])]},
                  {"content": "Created both."}]
        with FakeOllama(script) as fake:
            a = self.agent(fake)
            turn = a.turn("make a.md and b.md")
        self.assertEqual(turn.answer, "Created both.")
        self.assertTrue((self.ws / "a.md").exists())
        self.assertIn("2 changes", turn.ledger())
        tool_msg = fake.requests[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn('"ok": true', tool_msg["content"])

    def test_prose_claim_without_tool_is_visible_in_ledger(self):
        with FakeOllama([{"content": "Done! I created the file."}]) as fake:
            turn = self.agent(fake).turn("create notes.txt")
        self.assertFalse((self.ws / "notes.txt").exists())
        self.assertEqual(turn.ledger(), "◦ no tools used")

    def test_conservative_is_never_offered_write_tools(self):
        with FakeOllama([{"content": "ok"}]) as fake:
            self.agent(fake, access="conservative").turn("hi")
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        self.assertIn("read_file", names)
        self.assertNotIn("write_file", names)
        self.assertNotIn("run_command", names)
        self.assertNotIn("signal", names, "signal belongs to the Future Crash persona")

    def test_model_naming_an_unoffered_tool_is_denied(self):
        script = [{"tool_calls": [call("remove_path", path="x")]}, {"content": "could not"}]
        (self.ws / "x").write_text("keep")
        with FakeOllama(script) as fake:
            turn = self.agent(fake, access="conservative").turn("delete x")
        self.assertTrue((self.ws / "x").exists())
        self.assertEqual(turn.results[0][2].status, "denied")
        self.assertIn("did not happen", turn.ledger())

    def test_repeated_identical_calls_are_stopped(self):
        loop = {"tool_calls": [call("list_files", path=".")]}
        with FakeOllama([loop] * 6 + [{"content": "done"}]) as fake:
            turn = self.agent(fake).turn("look around")
        statuses = [r.status for _, _, r in turn.results]
        self.assertEqual(statuses[:2], ["ok", "ok"])
        self.assertIn("refused", statuses)

    def test_budget_spent_forces_answer_without_tools(self):
        from look.settings import settings
        settings.set("lo.max_rounds", 2)
        steps = [{"tool_calls": [call("list_files", path=f"sub{i}" if i else ".")]} for i in range(5)] + [{"content": "summary"}]
        (self.ws / "sub1").mkdir()
        with FakeOllama(steps) as fake:
            turn = self.agent(fake).turn("explore")
        self.assertEqual(turn.rounds, 3)
        self.assertNotIn("tools", fake.requests[-1])

    def test_inline_think_tags_do_not_reach_the_answer(self):
        with FakeOllama([{"content": "<think>secret plan</think>The answer."}]) as fake:
            turn = self.agent(fake).turn("q")
        self.assertEqual(turn.answer, "The answer.")
        self.assertIn("secret plan", turn.thinking)

    def test_history_never_starts_mid_transaction(self):
        from look.kernel import agent as agent_mod
        with FakeOllama([]) as fake:
            a = self.agent(fake)
        a.history = [{"role": "tool", "content": "x"}, {"role": "assistant", "content": "y"},
                     {"role": "user", "content": "z" * 10}, {"role": "assistant", "content": "ok"}]
        self.assertEqual(a._trimmed_history()[0]["role"], "user")

    def test_turn_queues_reflection_and_recent(self):
        from look.daemon import jobs
        from look.mind.memory import Mind
        with FakeOllama([{"content": "Noted."}]) as fake:
            self.agent(fake).turn("I prefer tabs over spaces")
        self.assertEqual(jobs.counts()["reflect"], 1)
        self.assertEqual(Mind("lo").recent()[-1]["user"], "I prefer tabs over spaces")

    def test_unattended_job_denies_instead_of_asking(self):
        outside = self.home / "elsewhere" / "x.txt"
        script = [{"tool_calls": [call("write_file", path=str(outside), content="x")]}, {"content": "blocked"}]
        with FakeOllama(script) as fake:
            turn = self.agent(fake).turn("write elsewhere")
        self.assertFalse(outside.exists())
        self.assertEqual(turn.results[0][2].status, "denied")

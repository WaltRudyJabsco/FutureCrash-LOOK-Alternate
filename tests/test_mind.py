from base import LookTest

import json

from fake_ollama import FakeOllama


class MemoryTests(LookTest):
    def test_reinforcement_not_duplication(self):
        from look.mind.memory import Mind
        m = Mind("lo")
        data = m.load()
        self.assertEqual(m.add(data, "Sasha prefers dark terminal themes", 60), "added")
        self.assertEqual(m.add(data, "Sasha prefers dark terminal themes always", 50), "reinforced")
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["importance"], 65)

    def test_decay_forgets_and_cap_evicts_weakest(self):
        from look.mind.memory import MAX_CANDIDATES, Mind
        m = Mind("lo")
        data = m.load()
        m.add(data, "a fading detail about lunch", 1)
        m.decay(data, touched=set())
        self.assertEqual(data["candidates"], [])
        words = ["amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
                 "kelp", "lumen", "mesa", "nectar", "onyx", "prism", "quartz", "raven", "sable", "tundra",
                 "umber", "vellum", "willow", "xenon", "yarrow"]
        for i, w in enumerate(words):
            m.add(data, f"likes {w} {w}s", 30 + i)
        self.assertEqual(len(data["candidates"]), MAX_CANDIDATES)
        self.assertNotIn("amber", " ".join(c["text"] for c in data["candidates"]))

    def test_attention_is_bounded_and_relevant(self):
        from look.mind.memory import ATTENTION, Mind
        m = Mind("lo")
        data = m.load()
        for i in range(15):
            m.add(data, f"novel chapter {i} draft notes kyoto{i}", 50)
        m.add(data, "uses a mechanical keyboard", 50)
        m.save(data)
        picked = m.relevant("how is the kyoto novel draft going")
        self.assertLessEqual(len(picked), ATTENTION)
        self.assertNotIn("keyboard", " ".join(c["text"] for c in picked))

    def test_retrieval_is_not_reinforcement(self):
        from look.mind.memory import Mind
        m = Mind("lo")
        data = m.load()
        m.add(data, "writes for a newspaper", 50)
        m.save(data)
        m.context("newspaper")
        self.assertEqual(m.load()["candidates"][0]["importance"], 50)


class ReflectionTests(LookTest):
    def reflect(self, reply, user="hello", summary_reply=None):
        from look.kernel.ollama import Ollama
        from look.mind.memory import Mind
        from look.mind.reflect import reflect
        replies = [reply] + ([summary_reply] if summary_reply else [])
        with FakeOllama(json_replies=replies) as fake:
            report = reflect(Mind("lo"), {"user": user, "assistant": "ok"}, Ollama(fake.url), "fake:1b")
        return report, Mind("lo").load(), fake

    def test_structured_output_schema_is_sent(self):
        _, _, fake = self.reflect(json.dumps({"memories": [], "skill": "", "durable": False}))
        self.assertIn("format", fake.requests[0])

    def test_valid_memories_are_applied(self):
        reply = json.dumps({"memories": [{"text": "Is writing a novel set in 1930s Kyoto", "importance": 75}],
                            "skill": "", "durable": False})
        report, data, _ = self.reflect(reply)
        self.assertEqual(report["added"], 1)
        self.assertEqual(data["candidates"][0]["importance"], 75)

    def test_garbage_is_rejected_but_explicit_request_survives(self):
        report, data, _ = self.reflect("<think>hmm</think> not json at all", user="Please remember this: my editor is Helix.")
        self.assertTrue(report.get("fallback"))
        self.assertIn("Helix", data["candidates"][0]["text"])

    def test_durable_triggers_summary_rewrite_with_validation(self):
        reply = json.dumps({"memories": [{"text": "Keeps a photography site", "importance": 90}], "skill": "", "durable": True})
        report, data, _ = self.reflect(reply, summary_reply="The user keeps a photography website.")
        self.assertTrue(report["summary"])
        self.assertEqual(data["summary"], "The user keeps a photography website.")
        too_long = " ".join(["word"] * 400)
        report, data, _ = self.reflect(reply, summary_reply=too_long)
        self.assertFalse(report["summary"])
        self.assertEqual(data["summary"], "The user keeps a photography website.")


class SkillsTests(LookTest):
    def test_specific_facts_are_not_craft(self):
        from look.mind import skills
        self.assertEqual(skills.learn("Always check ~/Downloads/report.pdf first"), "rejected")
        self.assertEqual(skills.learn("Use qwen3:8b for coding"), "rejected")
        self.assertEqual(skills.learn("Confirm the destination folder exists before moving many files"), "learned")
        self.assertEqual(skills.learn("Confirm the destination folder exists before moving files"), "duplicate")

    def test_bundled_refresh_preserves_learned(self):
        from look import store
        from look.mind import skills
        from look.paths import roots
        skills.add("Summarise long logs by error class before quoting lines")
        text = store.read_text(roots.skills).replace("bundled-version: 2", "bundled-version: 1")
        store.write_text(roots.skills, text.replace("- Inspect existing files before modifying them.\n", ""))
        skills.ensure()
        bundled, learned = skills.load()
        self.assertIn("Inspect existing files before modifying them.", bundled)
        self.assertEqual(len(learned), 1)

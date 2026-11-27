"""models.ask_choice and ask_spec: questions are answered with the levels' model at the questions' default reasoning,
unless the settings choose a model (and a reasoning level) of their own. An unavailable choice needs a new selection.
The catalog is a stand-in: nothing is scanned or called.

Run from the repo root:
    python3 -m unittest discover tests
"""
import unittest
from unittest import mock

from textfold import models

LEVELS = ["off", "low", "medium", "high", "xhigh", "max"]
CAT = [{"id": "claude/claude-code/sonnet", "name": "Sonnet 5.5", "kind": "agent", "group": "claude-code", "efforts": LEVELS, "target": "claude"},
       {"id": "claude/claude-code/opus", "name": "Opus 5.5", "kind": "agent", "group": "claude-code", "efforts": LEVELS, "target": "claude"},
       {"id": "openrouter/free", "name": "openrouter/free", "kind": "api", "group": "openrouter", "efforts": [], "target": "router"}]


def settings(**kw):
    return {"model": "claude/claude-code/sonnet", "model_target": "claude", "ask_model": None,
            "ask_effort": None, "efforts": {}, "providers": [], **kw}


class AskModel(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(models, "catalog", return_value=(CAT, []))
        p.start()
        self.addCleanup(p.stop)

    def test_none_chosen_the_levels_model_at_the_default(self):
        s = settings()
        self.assertEqual(models.ask_choice(s, CAT), (None, None))
        spec = models.ask_spec(s)
        self.assertEqual((spec["id"], spec["effort"]), ("claude/claude-code/sonnet", models.DEFAULT_THINKING["ask"]))

    def test_a_stronger_model_reasoning_more(self):
        spec = models.ask_spec(settings(ask_model="claude/claude-code/opus", ask_model_target="claude", ask_effort="high"))
        self.assertEqual((spec["id"], spec["model"], spec["effort"]), ("claude/claude-code/opus", "opus", "high"))

    def test_the_same_model_reasoning_more(self):
        spec = models.ask_spec(settings(ask_effort="xhigh"))
        self.assertEqual((spec["id"], spec["effort"]), ("claude/claude-code/sonnet", "xhigh"))

    def test_a_model_gone_or_a_level_it_has_not(self):
        gone = settings(ask_model="codex/codex/gone", ask_model_target="codex")
        self.assertEqual(models.ask_choice(gone, CAT), (None, None))
        with self.assertRaisesRegex(RuntimeError, "choose a model"):
            models.ask_spec(gone)
        self.assertEqual(models.ask_choice(settings(ask_model="openrouter/free", ask_model_target="router", ask_effort="high"), CAT),
                         ("openrouter/free", None))
        self.assertEqual(models.ask_choice(settings(ask_effort="ultra"), CAT), (None, None))   # (not one of Sonnet's)


if __name__ == "__main__":
    unittest.main()

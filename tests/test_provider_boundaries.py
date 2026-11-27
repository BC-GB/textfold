"""Provider origin, credential migration and executable discovery rules. No network or agents."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textfold import agents, llm, llm_openai, models, providers


class Transport(unittest.TestCase):
    def test_redirects_stay_on_the_selected_http_origin(self):
        origin = "https://model.example"
        request = urllib.request.Request(origin + "/models")
        redirect = llm_openai._ProviderRedirect().redirect_request
        response = io.BytesIO()
        destination = "https://MODEL.example:443/v1/models"
        self.assertEqual(redirect(request, response, 302, "", {}, destination).full_url, destination)
        for url in ("https://other.example/", "http://model.example/", "https://model.example:8443/",
                    "file:///tmp/model", "https://name@model.example/", "https://model.example:bad/"):
            with self.subTest(url=url), self.assertRaises(urllib.error.URLError):
                redirect(request, response, 302, "", {}, url)

    def test_native_anthropic_uses_the_selected_base_without_redirects(self):
        sdk = mock.MagicMock()
        client = sdk.Anthropic.return_value
        stream = client.beta.messages.stream.return_value.__enter__.return_value
        stream.get_final_message.return_value = SimpleNamespace(stop_reason="end_turn",
                                                               content=[SimpleNamespace(type="text", text="{}")])
        spec = {"model": "m", "base": "https://model.example", "api_key": "fixture"}
        with mock.patch.dict(sys.modules, {"anthropic": sdk}):
            self.assertEqual(llm._anthropic("sample", {}, spec, 5), ({}, None))
        sdk.DefaultHttpxClient.assert_called_once_with(follow_redirects=False)
        self.assertEqual(sdk.Anthropic.call_args.kwargs["base_url"], spec["base"])
        client.close.assert_called_once_with()


class ModelSelection(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(agents, "models", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def provider(self, base, model="study-model"):
        return {"key": "study", "name": "Study provider", "base": base, "protocol": "openai", "source": "ui",
                "api_key": "unused-fixture", "models": [{"id": model, "name": model}]}

    def test_saved_model_stays_with_its_destination(self):
        first = self.provider("http://127.0.0.1:11434/v1")
        other = self.provider("https://other.example/v1")
        with mock.patch.object(models, "provider_list", return_value=[first]) as available:
            rows, _ = models.catalog({"model": None})
            saved = {"model": rows[0]["id"], "model_target": rows[0]["target"]}
            self.assertEqual(models.resolve(s=saved)["base"], first["base"])
            available.return_value = [other]
            self.assertIsNone(models.default_model(saved))
            with self.assertRaisesRegex(RuntimeError, "choose a model"):
                models.resolve(s=saved)

    def test_matching_an_agent_style_id_does_not_change_the_kind(self):
        service = self.provider("http://127.0.0.1:11434/v1", "claude/claude-code/sonnet")
        with mock.patch.object(models, "provider_list", return_value=[service]):
            row = models.catalog({"model": None})[0][0]
            s = {"model": row["id"], "model_target": row["target"]}
            self.assertEqual(models.resolve(s=s)["kind"], "api")

    def test_a_legacy_id_without_a_saved_destination_needs_confirmation(self):
        with mock.patch.object(models, "catalog") as catalog:
            with self.assertRaisesRegex(RuntimeError, "choose a model"):
                models.resolve(s={"model": "study-model"})
        catalog.assert_not_called()


class LegacySettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.legacy = home / "legacy.env"
        self.current = home / "current.env"
        self.settings = home / "settings.json"
        for p in (mock.patch.object(models, "SETTINGS", self.settings), mock.patch.object(models, "OLD_HOMES", ()),
                  mock.patch.object(providers, "THOUGHTDAG_ENV", self.legacy),
                  mock.patch.object(providers, "ENV_FILES", [self.current]), mock.patch.dict(os.environ, {}, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_existing_private_settings_lose_group_and_other_access(self):
        self.settings.write_text('{"model": "selected"}')
        self.settings.chmod(0o644)
        self.assertEqual(models.load_settings()["model"], "selected")
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_absent_opt_in_does_not_read_another_projects_file(self):
        self.settings.write_text("{}")
        with mock.patch.object(providers, "_read_env_file", side_effect=AssertionError):
            self.assertEqual(models.load_settings()["providers"], [])

    def test_explicit_import_keeps_only_credentials_from_the_opted_in_source(self):
        self.settings.write_text(json.dumps({"use_thoughtdag_env": True}))
        self.current.write_text("MOONSHOT_API_KEY=current-fixture\n")
        self.legacy.write_text("MOONSHOT_MODELS=legacy-model\nMOONSHOT_BASE_URL=https://old.example\n"
                               "OPENAI_API_KEY=legacy-fixture\nOPENAI_MODELS=picked\n")
        s = models.load_settings()
        self.assertEqual([(p["key"], p["models"]) for p in s["providers"]], [("openai", [{"id": "picked"}])])
        self.assertFalse(s["use_thoughtdag_env"])
        self.assertNotIn("current-fixture", self.settings.read_text())
        if os.name != "nt":
            self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_model_free_mode_defers_a_requested_import(self):
        self.settings.write_text(json.dumps({"use_thoughtdag_env": True}))
        with mock.patch.dict(os.environ, {"TEXTFOLD_NO_MODELS": "1"}), mock.patch.object(models, "import_thoughtdag_env") as migrate:
            self.assertTrue(models.load_settings()["use_thoughtdag_env"])
            migrate.assert_not_called()

    def test_automatic_agent_search_uses_only_absolute_directories(self):
        paths = os.pathsep.join([".", str(self.current.parent)])
        with mock.patch.dict(os.environ, {"PATH": paths}), mock.patch.object(agents, "_login_path", "."), \
                mock.patch.object(agents, "COMMON", []), mock.patch("glob.glob", return_value=[]):
            self.assertEqual(agents.search_dirs(), [str(self.current.parent)])

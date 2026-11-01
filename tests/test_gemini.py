"""Provider regression checks: no credentials or paid network requests required."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from iltkb.cli import configured_model
from iltkb.engine import build, initialize
from iltkb.model import GeminiModel
from iltkb.storage import atomic_json, read_json
from test_system import ScriptedModel


class GeminiTests(unittest.TestCase):
    def test_missing_key_fails_with_actionable_message(self):
        with patch.dict(os.environ, {"ILTKB_TEST_ABSENT_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "environment"):
                GeminiModel(model="test", key_env="ILTKB_TEST_ABSENT_KEY")

    def make_model(self):
        if importlib.util.find_spec("langchain_google_genai") is None:
            self.skipTest("Install .[gemini] to exercise Gemini adapter")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "not-a-real-key"}):
            return GeminiModel(model="test-model", max_input_chars=1000)

    def test_langchain_json_messages_and_no_key_in_fingerprint(self):
        model = self.make_model()
        self.assertFalse(model.chat.vertexai)
        self.assertEqual(model.chat.response_mime_type, "application/json")
        class Chat:
            def invoke(self, messages):
                self.messages = messages
                return AIMessage(content='{"coverage_ok":true,"issues":[]}', response_metadata={"finish_reason": "STOP"})
        model.chat = Chat()
        self.assertTrue(model.complete("extract_review", {"text": "\u4e2d\u6587"})["coverage_ok"])
        self.assertIsInstance(model.chat.messages[0], SystemMessage)
        self.assertIsInstance(model.chat.messages[1], HumanMessage)
        self.assertNotIn("not-a-real-key", str(model.fingerprint))
        with self.assertRaisesRegex(ValueError, "budget"):
            model.complete("extract", {"text": "x" * 2000})

    def test_content_blocks_and_incomplete_responses(self):
        model = self.make_model()
        class Chat:
            response = AIMessage(content=[{"type": "text", "text": "private reasoning", "thought": True},
                                           {"type": "text", "text": '{"items":[]}'}],
                                 response_metadata={"finish_reason": "STOP"})
            def invoke(self, messages):
                return self.response
        model.chat = Chat()
        self.assertEqual(model.complete("extract", {}), {"items": []})
        for reason in ("MAX_TOKENS", "SAFETY", "RECITATION"):
            model.chat.response = AIMessage(content='{"items":[]}', response_metadata={"finish_reason": reason})
            with self.assertRaisesRegex(ValueError, "incomplete or blocked"):
                model.complete("extract", {})
        model.chat.response = AIMessage(content="not-json")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            model.complete("extract", {})

    def test_errors_do_not_echo_key_or_source(self):
        model = self.make_model()
        class Chat:
            def invoke(self, messages):
                raise RuntimeError("not-a-real-key and proprietary-source-contents")
        model.chat = Chat()
        with self.assertRaises(ValueError) as ctx:
            model.complete("extract", {"text": "proprietary-source-contents"})
        self.assertNotIn("not-a-real-key", str(ctx.exception))
        self.assertNotIn("proprietary-source-contents", str(ctx.exception))

    def test_default_gemini_and_old_ollama_config_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize(root)
            config = read_json(root / "iltkb.json")
            self.assertEqual(config["provider"], "gemini")
            config.update(model="test-model", model_revision="fixed-test-revision")
            atomic_json(root / "iltkb.json", config)
            with patch("iltkb.cli.GeminiModel") as gemini:
                configured_model(root)
                self.assertEqual(gemini.call_args.kwargs["model"], "test-model")
                self.assertEqual(gemini.call_args.kwargs["key_env"], "GEMINI_API_KEY")
            del config["provider"]
            atomic_json(root / "iltkb.json", config)
            with patch("iltkb.cli.OllamaModel") as ollama:
                configured_model(root)
                ollama.assert_called_once()

    def test_provider_change_invalidates_all_evidence_caches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize(root)
            (root / "raw" / "a.md").write_text("# test\nFor provider switching tests only.", encoding="utf-8")
            config = read_json(root / "iltkb.json")
            config["publication_policy"] = "auto"
            atomic_json(root / "iltkb.json", config)
            model = ScriptedModel()
            model.fingerprint = {"adapter": "ollama-test", "model": "same-name"}
            build(root, model)
            model.calls.clear()
            model.fingerprint = {"adapter": "gemini-test", "model": "same-name"}
            result = build(root, model)
            self.assertEqual(model.calls.count("extract"), 1)
            self.assertIn("optimization", result["rebuilt_topics"])


if __name__ == "__main__":
    unittest.main()

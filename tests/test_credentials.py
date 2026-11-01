import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from iltkb.credentials import resolve_api_key
from iltkb.cli import configured_model
from iltkb.engine import initialize
from iltkb.storage import atomic_json, read_json


class CredentialTests(unittest.TestCase):
    def test_target_value_formats_and_no_execution_or_interpolation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ILTKB_FAKE_KEY": ""}):
            path = Path(folder) / ".env"
            for line, expected in [
                ("ILTKB_FAKE_KEY=fake-value", "fake-value"),
                ('export ILTKB_FAKE_KEY = "fake value" # comment', "fake value"),
                ("ILTKB_FAKE_KEY='fake#value'", "fake#value"),
                ("ILTKB_FAKE_KEY=fake-value # comment", "fake-value"),
                ('ILTKB_FAKE_KEY="${HOME}$(ignored)"', "${HOME}$(ignored)"),
                ('ILTKB_FAKE_KEY="fake\\\"value"', 'fake"value'),
            ]:
                path.write_text("UNRELATED='invalid\n" + line, encoding="utf-8-sig")
                self.assertEqual(resolve_api_key("ILTKB_FAKE_KEY", path), expected)
            self.assertEqual(os.environ["ILTKB_FAKE_KEY"], "")

    def test_process_environment_precedes_file(self):
        with patch.dict(os.environ, {"ILTKB_FAKE_KEY": "from-environment"}):
            self.assertEqual(resolve_api_key("ILTKB_FAKE_KEY", Path("nonexistent.env")), "from-environment")

    def test_errors_never_echo_file_contents(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ILTKB_FAKE_KEY": ""}):
            path = Path(folder) / ".env"
            for text in ['ILTKB_FAKE_KEY="private-value',
                         "ILTKB_FAKE_KEY=private-value\nILTKB_FAKE_KEY=private-value",
                         "UNRELATED=private-value", "ILTKB_FAKE_KEY=''"]:
                path.write_text(text)
                with self.assertRaises(ValueError) as ctx:
                    resolve_api_key("ILTKB_FAKE_KEY", path)
                self.assertNotIn("private-value", str(ctx.exception))
            with self.assertRaisesRegex(ValueError, "Cannot read"):
                resolve_api_key("ILTKB_FAKE_KEY", Path(folder) / "missing")

    def test_config_path_is_relative_to_knowledge_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize(root)
            config = read_json(root / "iltkb.json")
            config.update(model="fake", model_revision="fake-revision", env_file="secrets/.env",
                          api_key_env="GOOGLE_API_KEY")
            atomic_json(root / "iltkb.json", config)
            with patch("iltkb.cli.GeminiModel") as model:
                configured_model(root)
                self.assertEqual(model.call_args.kwargs["env_file"], root / "secrets" / ".env")
                self.assertEqual(model.call_args.kwargs["key_env"], "GOOGLE_API_KEY")

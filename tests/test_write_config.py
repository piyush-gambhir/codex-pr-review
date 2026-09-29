import pathlib
import sys
import tempfile
import unittest

try:
    import tomllib
except ImportError:  # Python < 3.11: the config tests need a TOML parser
    tomllib = None

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import write_config as wc  # noqa: E402

BASE = {"CODEX_PROVIDER": "openai", "MODEL": "gpt-6.1-sol", "REASONING_EFFORT": "medium", "SANDBOX": "read-only"}


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class WriteConfigTest(unittest.TestCase):
    def test_minimal_config(self):
        cfg = tomllib.loads(wc.build_config(BASE))
        self.assertEqual(cfg["model_provider"], "openai")
        self.assertEqual(cfg["approval_policy"], "never")
        self.assertEqual(cfg["shell_environment_policy"]["inherit"], "core")
        self.assertNotIn("developer_instructions", cfg)

    def test_instructions_survive_quotes_and_newlines(self):
        text = 'Flag "unscoped" SQL.\nIgnore src/gen/**.\tTabs too. \\ backslash \U0001F512'
        cfg = tomllib.loads(wc.build_config({**BASE, "REVIEW_INSTRUCTIONS": text}))
        self.assertIn(text, cfg["developer_instructions"])
        self.assertIn("Full review comments:", cfg["developer_instructions"])

    def test_instructions_file_and_extra_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "REVIEW.md").write_text("Check tenant scoping.")
            env = {**BASE, "GITHUB_WORKSPACE": tmp, "REVIEW_INSTRUCTIONS_FILE": "REVIEW.md",
                   "CODEX_CONFIG": 'model_verbosity = "low"'}
            cfg = tomllib.loads(wc.build_config(env))
        self.assertIn("Check tenant scoping.", cfg["developer_instructions"])
        self.assertEqual(cfg["model_verbosity"], "low")

    def test_missing_instructions_file(self):
        with self.assertRaises(SystemExit):
            wc.build_config({**BASE, "GITHUB_WORKSPACE": "/nonexistent", "REVIEW_INSTRUCTIONS_FILE": "x.md"})


if __name__ == "__main__":
    unittest.main()

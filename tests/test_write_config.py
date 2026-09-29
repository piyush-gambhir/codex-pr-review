import pathlib
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import write_config as wc  # noqa: E402

BASE = {"CODEX_PROVIDER": "openai", "MODEL": "gpt-6.1-sol", "REASONING_EFFORT": "medium", "SANDBOX": "read-only"}


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

    def test_suggestion_guidance_only_when_asked_for(self):
        cfg = tomllib.loads(wc.build_config({**BASE, "SUGGESTIONS": "true"}))
        self.assertIn("tagged 'suggestion'", cfg["developer_instructions"])
        self.assertIn("Full review comments:", cfg["developer_instructions"])
        self.assertNotIn("developer_instructions", tomllib.loads(wc.build_config({**BASE, "SUGGESTIONS": "false"})))

    def test_excluded_paths_are_passed_on_to_codex(self):
        cfg = tomllib.loads(wc.build_config({**BASE, "EXCLUDE_PATHS": "**/gen/**, *.lock"}))
        self.assertIn("**/gen/**, *.lock", cfg["developer_instructions"])
        self.assertNotIn("tagged 'suggestion'", cfg["developer_instructions"])

    def test_guidelines_exclusions_and_suggestions_together(self):
        env = {**BASE, "REVIEW_INSTRUCTIONS": "Money is integer cents.",
               "EXCLUDE_PATHS": "src/gen/**", "SUGGESTIONS": "true"}
        text = tomllib.loads(wc.build_config(env))["developer_instructions"]
        for expected in ("Money is integer cents.", "src/gen/**", "tagged 'suggestion'", "Full review comments:"):
            self.assertIn(expected, text)

    def test_missing_instructions_file(self):
        with self.assertRaises(SystemExit):
            wc.build_config({**BASE, "GITHUB_WORKSPACE": "/nonexistent", "REVIEW_INSTRUCTIONS_FILE": "x.md"})


if __name__ == "__main__":
    unittest.main()

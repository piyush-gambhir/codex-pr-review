import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import history  # noqa: E402
import publish_review as pr  # noqa: E402
import rereview  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
OLD_SHA = "89abcdef0123456789abcdef0123456789abcdef"
URL = "https://github.com/o/r/pull/1#issuecomment-7"

SETTINGS = {
    "PROVIDER": "openai", "MODEL": "gpt-6.1-sol", "REASONING_EFFORT": "medium",
    "BASE_REF": "origin/main", "REVIEW_MODE": "single", "INCREMENTAL": "false",
    "SUGGESTIONS": "true", "MAX_PRIORITY": "P3", "POST_MODE": "review",
    "INCLUDE_PATHS": "", "EXCLUDE_PATHS": "",
    "REVIEW_INSTRUCTIONS": "", "REVIEW_INSTRUCTIONS_FILE": "",
}


class SettingsDigestTest(unittest.TestCase):
    def digest(self, **changes):
        return history.settings_digest({**SETTINGS, **changes})

    def test_same_settings_give_the_same_digest(self):
        self.assertEqual(self.digest(), self.digest())
        self.assertEqual(len(self.digest()), 12)

    def test_cosmetics_do_not_count(self):
        # Nothing here changes what a review says.
        self.assertEqual(self.digest(), self.digest(REVIEW_TITLE="Something else"))
        self.assertEqual(self.digest(), self.digest(RERUN_HINT="new hint"))
        self.assertEqual(self.digest(), self.digest(HIDE_PREVIOUS="false"))
        self.assertEqual(self.digest(), self.digest(RUN_URL="https://example.test/9"))

    def test_case_and_pattern_order_do_not_count(self):
        self.assertEqual(self.digest(), self.digest(REASONING_EFFORT="MEDIUM"))
        self.assertEqual(self.digest(MAX_PRIORITY="p2"), self.digest(MAX_PRIORITY="P2"))
        self.assertEqual(
            self.digest(EXCLUDE_PATHS="**/gen/**\n*.lock"),
            self.digest(EXCLUDE_PATHS="*.lock, **/gen/**"),
        )

    def test_everything_that_changes_the_review_counts(self):
        base = self.digest()
        for change in (
            {"PROVIDER": "bedrock"},
            {"MODEL": "gpt-6-astra"},
            {"REASONING_EFFORT": "high"},
            {"BASE_REF": "origin/develop"},
            {"INCREMENTAL": "true"},
            {"SUGGESTIONS": "false"},
            {"MAX_PRIORITY": "P1"},
            {"POST_MODE": "comment"},
            {"INCLUDE_PATHS": "src/**"},
            {"EXCLUDE_PATHS": "*.lock"},
            {"REVIEW_INSTRUCTIONS": "Be strict about money."},
            # A single review cannot satisfy a request for a full one.
            {"REVIEW_MODE": "full"},
        ):
            self.assertNotEqual(base, self.digest(**change), change)

    def test_a_full_review_and_a_single_one_are_not_interchangeable(self):
        self.assertNotEqual(self.digest(REVIEW_MODE="single"), self.digest(REVIEW_MODE="full"))
        self.assertEqual(self.digest(REVIEW_MODE="FULL"), self.digest(REVIEW_MODE="full"))
        # `auto` is resolved to one of the two before the digest is taken, so the
        # digest never carries it.
        self.assertEqual(self.digest(REVIEW_MODE=""), self.digest(REVIEW_MODE=" "))

    def test_a_guidelines_file_counts_by_its_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            guide = pathlib.Path(tmp, "codex-review.md")
            guide.write_text("Money is integer cents.", encoding="utf-8")
            env = {"GITHUB_WORKSPACE": tmp, "REVIEW_INSTRUCTIONS_FILE": "codex-review.md"}
            first = self.digest(**env)
            guide.write_text("Money is integer cents. Tenancy is P0.", encoding="utf-8")
            self.assertNotEqual(first, self.digest(**env))
            # A file that is configured but missing is its own case, never a match.
            guide.unlink()
            self.assertNotIn(self.digest(**env), (first, self.digest()))


class DecisionTest(unittest.TestCase):
    def previous(self, sha=SHA, cfg="abc123456789"):
        return {"sha": sha, "cfg": cfg, "findings": []}

    def test_same_commit_and_settings_skips(self):
        self.assertEqual(rereview.decision(self.previous(), SHA, "abc123456789"),
                         (True, "already-reviewed"))

    def test_a_new_commit_reviews(self):
        self.assertEqual(rereview.decision(self.previous(OLD_SHA), SHA, "abc123456789"),
                         (False, "new-commit"))

    def test_changed_settings_review(self):
        self.assertEqual(rereview.decision(self.previous(), SHA, "different1234"),
                         (False, "settings-changed"))

    def test_a_marker_without_a_digest_reviews(self):
        self.assertEqual(rereview.decision({"sha": SHA, "findings": []}, SHA, "abc123456789"),
                         (False, "no-settings-digest"))

    def test_no_previous_review_reviews(self):
        self.assertEqual(rereview.decision({}, SHA, "abc123456789"), (False, "no-previous-review"))

    def test_force_and_the_switch_win(self):
        self.assertEqual(rereview.decision(self.previous(), SHA, "abc123456789", force=True),
                         (False, "forced"))
        self.assertEqual(rereview.decision(self.previous(), SHA, "abc123456789", enabled=False),
                         (False, "disabled"))

    def test_an_unknown_head_never_skips(self):
        self.assertEqual(rereview.decision(self.previous(""), "", "abc123456789"),
                         (False, "new-commit"))


def run_check(previous, **env):
    """Run rereview.main('check') against a plan file; returns (outputs, calls)."""
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp, "state.json")
        state.write_text(json.dumps({"previous": previous}), encoding="utf-8")
        out = pathlib.Path(tmp, "out")
        out.touch()
        full = {
            "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1", "GH_TOKEN": "t", "HEAD_SHA": SHA,
            "STATE_FILE": str(state), "GITHUB_OUTPUT": str(out), "REVIEW_TITLE": "Codex review",
            "LABEL": "OpenAI API", "RUN_URL": "https://github.com/o/r/actions/runs/9",
            **SETTINGS,
        }
        full.update(env)
        api = mock.Mock(side_effect=lambda *a, **k: calls.append(a) or {"id": 5})
        # Every module keeps its own reference to publish_review.github.
        with mock.patch.dict(os.environ, full, clear=True), \
                mock.patch.object(pr, "github", api), \
                mock.patch.object(rereview, "github", api), \
                mock.patch.object(rereview.status, "github", api):
            code = rereview.main("check")
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if line)
    return code, outputs, calls


def state(sha=SHA, **changes):
    """A previous review's state with the digest the given settings produce."""
    return {"sha": sha, "cfg": history.settings_digest({**SETTINGS, **changes}), "findings": [],
            "url": URL}


class MainTest(unittest.TestCase):
    def test_a_skip_posts_the_note_and_reacts(self):
        code, outputs, calls = run_check(state(), TRIGGER_COMMENT_ID="99")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["skipped"], "true")
        self.assertEqual(outputs["skip-reason"], "already-reviewed")
        self.assertEqual(outputs["review-url"], URL)
        posted = [c for c in calls if c[1].endswith("/issues/1/comments")]
        self.assertEqual(len(posted), 1)
        body = posted[0][3]["body"]
        self.assertIn("Codex review skipped", body)
        self.assertIn(URL, body)
        self.assertIn("Add `force` to the request", body)
        self.assertIn(SHA[:7], body)
        # The status marker, so the next real review collapses this note.
        self.assertTrue(body.startswith("<!-- codex-pr-review-status -->"))
        self.assertIn(("POST", "/repos/o/r/issues/comments/99/reactions", "t", {"content": "rocket"}),
                      [c for c in calls if "reactions" in c[1]])

    def test_a_note_without_a_link_still_reads(self):
        previous = state()
        previous.pop("url")
        _, outputs, calls = run_check(previous)
        self.assertEqual(outputs["review-url"], "")
        self.assertIn("already has a Codex review", calls[0][3]["body"])

    def test_no_skip_posts_nothing(self):
        for previous in ({}, state(OLD_SHA), state(REASONING_EFFORT="high")):
            code, outputs, calls = run_check(previous)
            self.assertEqual((code, outputs["skipped"], calls), (0, "false", []))

    def test_force_posts_nothing(self):
        _, outputs, calls = run_check(state(), FORCE="true")
        self.assertEqual((outputs["skipped"], outputs["skip-reason"], calls), ("false", "", []))

    def test_turning_it_off_posts_nothing(self):
        _, outputs, calls = run_check(state(), SKIP_UNCHANGED="false")
        self.assertEqual((outputs["skipped"], calls), ("false", []))

    def test_post_mode_none_skips_without_a_note(self):
        _, outputs, calls = run_check(state(POST_MODE="none"), POST_MODE="none")
        self.assertEqual((outputs["skipped"], calls), ("true", []))

    def test_unknown_action(self):
        self.assertEqual(rereview.main("nope"), 1)


class RoundTripTest(unittest.TestCase):
    """What publish_review.py writes is what the next run's check reads."""

    def test_the_marker_a_review_posts_is_recognised(self):
        env = {**SETTINGS, "HEAD_SHA": SHA}
        marker = history.state_marker(SHA, [], [], history.settings_digest(env))
        previous = history.parse_state(f"## Codex review\n\nno issues\n\n{marker}")
        self.assertEqual(
            rereview.decision(previous, SHA, history.settings_digest(env)),
            (True, "already-reviewed"),
        )
        # And a run with one setting changed does not reuse it.
        self.assertEqual(
            rereview.decision(previous, SHA, history.settings_digest({**env, "MAX_PRIORITY": "P1"})),
            (False, "settings-changed"),
        )

    def test_a_marker_with_no_settings_is_still_readable(self):
        marker = history.state_marker(SHA, [])
        self.assertNotIn("cfg", history.parse_state(marker))


if __name__ == "__main__":
    unittest.main()

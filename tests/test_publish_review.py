import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import publish_review as pr  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
WORKSPACE = "/home/runner/work/repo/repo"
SHA = "0123456789abcdef0123456789abcdef01234567"


class ParseTest(unittest.TestCase):
    def test_real_codex_output(self):
        # Captured from `codex exec review` 0.159.1 on a branch with two planted bugs.
        summary, findings = pr.parse_review((FIXTURES / "review-two-findings.md").read_text(), WORKSPACE)
        self.assertTrue(summary.startswith("The discount calculation"))
        self.assertEqual([f["priority"] for f in findings], [1, 2])
        self.assertEqual({f["path"] for f in findings}, {"src/pricing.ts"})
        self.assertEqual([(f["start"], f["end"]) for f in findings], [(15, 15), (22, 22)])
        self.assertIn("divide `percent` by 100", findings[0]["body"])

    def test_prefixed_titles_and_relative_paths(self):
        # Captured output where custom instructions added a word before the tag.
        _, findings = pr.parse_review((FIXTURES / "review-prefixed-relative.md").read_text(), WORKSPACE)
        self.assertEqual(len(findings), 3)
        self.assertTrue(all(f["title"].startswith("ZEBRA ") for f in findings))
        self.assertEqual(findings[2]["path"], "src/pricing.ts")
        self.assertEqual((findings[2]["start"], findings[2]["end"]), (11, 12))

    def test_no_findings(self):
        summary, findings = pr.parse_review("No issues found in the changes.", WORKSPACE)
        self.assertEqual((summary, findings), ("No issues found in the changes.", []))

    def test_multiline_body(self):
        text = f"S.\n\nFull review comments:\n\n- [P0] Crash \u2014 {WORKSPACE}/a/b.py:3-7\n  One.\n  Two.\n"
        _, findings = pr.parse_review(text, WORKSPACE)
        self.assertEqual((findings[0]["path"], findings[0]["body"]), ("a/b.py", "One.\nTwo."))

    def test_priority_values(self):
        self.assertEqual(pr.parse_priority("P1", None), 1)
        self.assertEqual(pr.parse_priority("2", None), 2)
        self.assertEqual(pr.parse_priority("", 3), 3)
        self.assertIsNone(pr.parse_priority("none", None))
        with self.assertRaises(SystemExit):
            pr.parse_priority("P7", None)


def ctx(**extra):
    env = {"GITHUB_REPOSITORY": "o/r", "HEAD_SHA": SHA, "BASE_REF": "origin/main", "MODEL": "gpt-6.1-sol",
           "LABEL": "OpenAI API", "REASONING_EFFORT": "medium", "RUN_URL": "https://github.com/o/r/actions/runs/9",
           "RERUN_HINT": "Comment `@gpt review` to re-run."}
    env.update(extra)
    return pr.Context(env)


class RenderTest(unittest.TestCase):
    def setUp(self):
        _, self.findings = pr.parse_review((FIXTURES / "review-two-findings.md").read_text(), WORKSPACE)

    def test_location_links_to_lines_at_commit(self):
        f = dict(self.findings[0], start=15, end=18)
        self.assertEqual(
            pr.location(f, ctx()),
            f"[`src/pricing.ts:15-18`](https://github.com/o/r/blob/{SHA}/src/pricing.ts#L15-L18)",
        )

    def test_verdicts(self):
        self.assertIn("No issues found", pr.verdict([]))
        self.assertIn("2 issues to address", pr.verdict(self.findings))
        self.assertIn("(1 P1, 1 P2)", pr.verdict(self.findings))
        self.assertIn("1 minor issue", pr.verdict([self.findings[1]]))

    def test_body_lists_every_issue_and_collapses_non_inline(self):
        body = pr.body_markdown("Summary.", self.findings, ctx(), {id(self.findings[0])})
        self.assertTrue(body.startswith(pr.MARKER))
        self.assertIn("| \U0001f7e0 | P1 | Convert the percentage", body)
        self.assertIn("\U0001f4ac inline", body)
        details = body.split("**Details**", 1)[1]
        self.assertIn("<details>", details)
        self.assertIn("Handle zero units", details)
        self.assertNotIn("Convert the percentage", details)

    def test_meta_and_cta_footer(self):
        body = pr.body_markdown("Summary.", [], ctx())
        self.assertIn(f"Reviewed [`{SHA[:7]}`](https://github.com/o/r/commit/{SHA}) against `main`", body)
        self.assertIn("`gpt-6.1-sol` via OpenAI API", body)
        self.assertIn("[View run](https://github.com/o/r/actions/runs/9)", body)
        self.assertIn("Comment `@gpt review` to re-run.", body)

    def test_inline_comment_links_back(self):
        text = pr.inline_comment(self.findings[0], ctx())
        self.assertIn("**P1 \u00b7 Convert the percentage", text)
        self.assertIn("#L15", text)
        self.assertTrue(text.rstrip().endswith(pr.MARKER))

    def test_html_in_titles_is_escaped_in_details(self):
        f = dict(self.findings[0], title="Use <T> & friends")
        self.assertIn("Use &lt;T&gt; &amp; friends", pr.details(f, ctx()))

    def test_pipe_in_title_is_escaped(self):
        f = dict(self.findings[0], title="a | b")
        self.assertIn("a \\| b", pr.issues_table([f], ctx(), set()))


class MainTest(unittest.TestCase):
    def run_main(self, review_text, **env):
        with tempfile.TemporaryDirectory() as tmp:
            review = pathlib.Path(tmp, "review.md")
            review.write_text(review_text)
            out, summary = pathlib.Path(tmp, "out"), pathlib.Path(tmp, "summary")
            base = {
                "REVIEW_FILE": str(review), "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1",
                "HEAD_SHA": SHA, "REVIEW_WORKSPACE": WORKSPACE, "POST_MODE": "none",
                "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": tmp,
            }
            with mock.patch.dict(os.environ, {**base, **env}, clear=True):
                code = pr.main()
            outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
            findings = json.loads(pathlib.Path(outputs["findings-file"]).read_text())
            return code, outputs, findings, summary.read_text()

    def test_outputs_and_summary(self):
        code, outputs, findings, summary = self.run_main((FIXTURES / "review-two-findings.md").read_text())
        self.assertEqual(code, 0)
        self.assertEqual((outputs["findings-count"], outputs["highest-priority"]), ("2", "P1"))
        self.assertEqual(len(findings), 2)
        self.assertIn("2 issues to address", summary)
        self.assertNotIn(pr.MARKER, summary)

    def test_max_priority_filters(self):
        _, outputs, findings, summary = self.run_main((FIXTURES / "review-two-findings.md").read_text(), MAX_PRIORITY="P1")
        self.assertEqual((outputs["findings-count"], outputs["filtered-count"]), ("1", "1"))
        self.assertIn("1 lower-priority finding below P1 not shown", summary)

    def test_fail_on_priority(self):
        text = (FIXTURES / "review-two-findings.md").read_text()
        self.assertEqual(self.run_main(text, FAIL_ON_PRIORITY="P1")[0], 1)
        self.assertEqual(self.run_main(text, FAIL_ON_PRIORITY="P0")[0], 0)

    def test_empty_review_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = pathlib.Path(tmp, "r.md")
            review.write_text("   ")
            with mock.patch.dict(os.environ, {"REVIEW_FILE": str(review)}, clear=True):
                self.assertEqual(pr.main(), 1)

    def test_posts_single_comment_mode(self):
        calls = []
        with mock.patch.object(pr, "github", side_effect=lambda *a, **k: calls.append(a) or []):
            self.run_main((FIXTURES / "review-two-findings.md").read_text(), POST_MODE="comment", GH_TOKEN="t")
        posts = [c for c in calls if c[0] == "POST" and c[1].endswith("/issues/1/comments")]
        self.assertEqual(len(posts), 1)
        self.assertIn("2 issues to address", posts[0][3]["body"])
        self.assertIn("(click to expand)", posts[0][3]["body"])


if __name__ == "__main__":
    unittest.main()

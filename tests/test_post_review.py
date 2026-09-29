import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import post_review  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
WORKSPACE = "/home/runner/work/repo/repo"


class ParseReviewTest(unittest.TestCase):
    def test_parses_real_codex_output(self):
        # Captured from `codex exec review` 0.159.1 on a branch with two planted bugs.
        text = (FIXTURES / "review-two-findings.md").read_text()
        summary, findings = post_review.parse_review(text, WORKSPACE)
        self.assertTrue(summary.startswith("The discount calculation"))
        self.assertEqual([f["priority"] for f in findings], [1, 2])
        self.assertEqual({f["path"] for f in findings}, {"src/pricing.ts"})
        self.assertEqual([(f["start"], f["end"]) for f in findings], [(15, 15), (22, 22)])
        self.assertIn("divide `percent` by 100", findings[0]["body"])

    def test_no_findings_section(self):
        summary, findings = post_review.parse_review("No issues found in the changes.", WORKSPACE)
        self.assertEqual(findings, [])
        self.assertEqual(summary, "No issues found in the changes.")

    def test_multiline_range_and_multiline_body(self):
        text = (
            "Summary.\n\nFull review comments:\n\n"
            f"- [P0] Break on null \u2014 {WORKSPACE}/a/b.py:3-7\n"
            "  First line.\n  Second line.\n"
        )
        _, findings = post_review.parse_review(text, WORKSPACE)
        self.assertEqual(findings[0]["path"], "a/b.py")
        self.assertEqual((findings[0]["start"], findings[0]["end"]), (3, 7))
        self.assertEqual(findings[0]["body"], "First line.\nSecond line.")


if __name__ == "__main__":
    unittest.main()

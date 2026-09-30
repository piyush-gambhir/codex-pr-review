import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import checks  # noqa: E402
import publish_review as pr  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
ENV = {"GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1", "GH_TOKEN": "t", "HEAD_SHA": SHA,
       "BASE_REF": "origin/main", "REVIEW_TITLE": "Codex review", "MODEL": "gpt-6.1-sol",
       "LABEL": "OpenAI API", "REASONING_EFFORT": "medium", "RUN_URL": "https://github.com/o/r/actions/runs/9"}


def finding(priority=1, start=15, end=15, title="Convert the percentage", body="Because."):
    return {"priority": priority, "title": title, "path": "src/pricing.ts", "start": start, "end": end, "body": body}


def ctx(**extra):
    env = dict(ENV)
    env.update(extra)
    return pr.Context(env)


class MappingTest(unittest.TestCase):
    def test_annotation_levels(self):
        self.assertEqual([checks.annotation_level(p) for p in range(4)], ["failure", "failure", "warning", "notice"])

    def test_conclusion_without_findings_is_success(self):
        self.assertEqual(checks.conclusion([], None), "success")
        self.assertEqual(checks.conclusion([], 1), "success")

    def test_conclusion_is_neutral_when_gating_is_off_or_not_reached(self):
        self.assertEqual(checks.conclusion([finding(2)], None), "neutral")
        self.assertEqual(checks.conclusion([finding(2)], 1), "neutral")
        self.assertEqual(checks.conclusion([finding(3)], 3), "failure")

    def test_conclusion_is_failure_when_gating_trips(self):
        self.assertEqual(checks.conclusion([finding(1), finding(3)], 1), "failure")
        self.assertEqual(checks.conclusion([finding(0)], 1), "failure")

    def test_annotations_carry_lines_level_and_text(self):
        items = checks.annotations([finding(0, 15, 18), finding(3, 22, 22, body="")])
        self.assertEqual(items[0], {
            "path": "src/pricing.ts", "start_line": 15, "end_line": 18, "annotation_level": "failure",
            "title": "P0: Convert the percentage", "message": "Because.",
        })
        self.assertEqual(items[1]["annotation_level"], "notice")
        # An empty body falls back to the title, since GitHub requires a message.
        self.assertEqual(items[1]["message"], "Convert the percentage")

    def test_annotation_lines_and_text_are_clamped(self):
        items = checks.annotations([finding(1, 0, 0, title="T" * 400, body="B" * 20000)])
        self.assertEqual((items[0]["start_line"], items[0]["end_line"]), (1, 1))
        self.assertEqual(len(items[0]["title"]), checks.TITLE_LIMIT)
        self.assertEqual(len(items[0]["message"]), checks.MESSAGE_LIMIT)


class RenderTest(unittest.TestCase):
    def test_headline_counts_priorities(self):
        self.assertEqual(checks.headline([]), "No issues found")
        self.assertEqual(checks.headline([finding(1), finding(1), finding(2)]), "3 issues (2 P1, 1 P2)")
        self.assertEqual(checks.headline([finding(2)]), "1 issue (1 P2)")

    def test_summary_reuses_the_issues_table_without_the_inline_column(self):
        summary = checks.summary_markdown("Codex prose.", [finding(1)], ctx())
        self.assertIn("> [!CAUTION]", summary)
        self.assertIn("1 issue to address", summary)
        self.assertIn("Codex prose.", summary)
        self.assertIn("|  | Priority | Issue | Location |", summary)
        self.assertNotIn("Where", summary)
        self.assertNotIn("inline", summary)
        self.assertIn(f"Reviewed [`{SHA[:7]}`]", summary)

    def test_summary_is_text_only_without_icons(self):
        summary = checks.summary_markdown("Codex prose.", [finding(1)], ctx(ICONS="false"))
        self.assertNotIn("<img", summary)
        self.assertIn("| Priority | Issue | Location |", summary)

    def test_in_progress_and_failure_summaries_are_alert_blocks(self):
        payloads = []
        with mock.patch.object(checks, "call", side_effect=lambda *a: payloads.append(a[3]) or {"id": 1}):
            checks.start("o/r", "t", ctx())
        self.assertIn("> [!NOTE]", payloads[0]["output"]["summary"])
        self.assertIn('alt="In progress"', payloads[0]["output"]["summary"])

    def test_summary_is_truncated_to_the_api_limit(self):
        summary = checks.summary_markdown("x" * 80000, [finding(1)], ctx())
        self.assertEqual(len(summary), checks.SUMMARY_LIMIT)
        self.assertTrue(summary.endswith(checks.SUMMARY_NOTE))

    def test_empty_summary_still_has_a_verdict(self):
        self.assertIn("No issues found", checks.summary_markdown("", [], ctx()))


class ApiTest(unittest.TestCase):
    def run_main(self, action, calls, responses=None, **env):
        responses = list(responses or [{"id": 42}])

        def fake(method, path, token, payload=None, url=None):
            calls.append((method, path, payload))
            return responses.pop(0) if responses else {"id": 42}

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp, "out")
            full = {**ENV, "GITHUB_OUTPUT": str(out)}
            full.update(env)
            with mock.patch.dict(os.environ, full, clear=True), mock.patch.object(checks, "github", side_effect=fake):
                code = checks.main(action)
            return code, out.read_text() if out.exists() else ""

    def findings_file(self, tmp, findings):
        path = pathlib.Path(tmp, "findings.json")
        path.write_text(json.dumps(findings))
        return str(path)

    def test_start_creates_an_in_progress_run_and_outputs_the_id(self):
        calls = []
        _, outputs = self.run_main("start", calls)
        self.assertEqual(calls[0][:2], ("POST", "/repos/o/r/check-runs"))
        payload = calls[0][2]
        self.assertEqual((payload["name"], payload["head_sha"], payload["status"]), ("Codex review", SHA, "in_progress"))
        self.assertEqual(payload["details_url"], ENV["RUN_URL"])
        self.assertIn("Reviewing", payload["output"]["summary"])
        self.assertIn("check-run-id=42", outputs)

    def test_start_warns_and_never_fails_without_the_permission(self):
        body = io.BytesIO(b'{"message": "Resource not accessible by integration"}')
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, body)
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp, "out")
            with mock.patch.dict(os.environ, {**ENV, "GITHUB_OUTPUT": str(out)}, clear=True), \
                    mock.patch.object(checks, "github", side_effect=error):
                self.assertEqual(checks.main("start"), 0)
            self.assertEqual(out.read_text() if out.exists() else "", "")

    def test_finish_completes_with_the_conclusion_and_annotations(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            findings = self.findings_file(tmp, [finding(1), finding(2, 22, 22)])
            self.run_main("finish", calls, CHECK_RUN_ID="42", FINDINGS_FILE=findings, FAIL_ON_PRIORITY="P1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("PATCH", "/repos/o/r/check-runs/42"))
        payload = calls[0][2]
        self.assertEqual((payload["status"], payload["conclusion"]), ("completed", "failure"))
        self.assertEqual(payload["output"]["title"], "2 issues (1 P1, 1 P2)")
        self.assertEqual(len(payload["output"]["annotations"]), 2)

    def test_finish_batches_annotations_fifty_at_a_time(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            findings = self.findings_file(tmp, [finding(2, i + 1, i + 1) for i in range(120)])
            self.run_main("finish", calls, CHECK_RUN_ID="42", FINDINGS_FILE=findings)
        self.assertEqual(len(calls), 3)
        self.assertEqual([len(c[2]["output"]["annotations"]) for c in calls], [50, 50, 20])
        self.assertTrue(all(c[2]["conclusion"] == "neutral" for c in calls))
        self.assertTrue(all(c[2]["output"]["title"] for c in calls))

    def test_finish_sends_one_request_when_there_are_no_findings(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            self.run_main("finish", calls, CHECK_RUN_ID="42", FINDINGS_FILE=self.findings_file(tmp, []))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["conclusion"], "success")
        self.assertEqual(calls[0][2]["output"]["annotations"], [])

    def test_finish_stops_batching_after_an_api_error(self):
        calls = []

        def fail(method, path, token, payload=None, url=None):
            calls.append((method, path, payload))
            raise urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(b"bad annotation"))

        with tempfile.TemporaryDirectory() as tmp:
            findings = self.findings_file(tmp, [finding(2, i + 1, i + 1) for i in range(120)])
            with mock.patch.dict(os.environ, {**ENV, "CHECK_RUN_ID": "42", "FINDINGS_FILE": findings}, clear=True), \
                    mock.patch.object(checks, "github", side_effect=fail):
                self.assertEqual(checks.main("finish"), 0)
        self.assertEqual(len(calls), 1)

    def test_finish_without_findings_reports_the_review_failure(self):
        calls = []
        events = None
        with tempfile.TemporaryDirectory() as tmp:
            events = pathlib.Path(tmp, "events.jsonl")
            events.write_text(json.dumps({"type": "error", "message": "model not found"}))
            self.run_main("finish", calls, CHECK_RUN_ID="42", EVENTS_FILE=str(events))
        self.assertEqual(calls[0][2]["conclusion"], "failure")
        self.assertEqual(calls[0][2]["output"]["title"], "Review failed")
        self.assertIn("model not found", calls[0][2]["output"]["summary"])

    def test_cancel_completes_as_cancelled(self):
        calls = []
        self.run_main("cancel", calls, CHECK_RUN_ID="42")
        self.assertEqual(calls[0][2]["conclusion"], "cancelled")

    def test_nothing_happens_without_a_check_run_id(self):
        calls = []
        self.assertEqual(self.run_main("finish", calls)[0], 0)
        self.assertEqual(calls, [])

    def test_unknown_action_fails(self):
        calls = []
        self.assertEqual(self.run_main("wat", calls)[0], 1)


if __name__ == "__main__":
    unittest.main()

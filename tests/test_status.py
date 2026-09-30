import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import status  # noqa: E402

ENV = {"GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "5", "GH_TOKEN": "t", "HEAD_SHA": "a" * 40,
       "REVIEW_TITLE": "Codex review", "RUN_URL": "https://example/run"}


class StatusTest(unittest.TestCase):
    def events(self, *events):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        tmp.write("\n".join(json.dumps(e) for e in events))
        tmp.close()
        return tmp.name

    def test_failure_reason_skips_retries(self):
        path = self.events(
            {"type": "error", "message": "Reconnecting... 1/5 (401)"},
            {"type": "error", "message": "unexpected status 401 Unauthorized: bad key"},
            {"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: bad key"}},
        )
        self.assertEqual(status.failure_reason(path), "unexpected status 401 Unauthorized: bad key")
        self.assertEqual(status.failure_reason("/nonexistent"), "")

    def test_start_posts_note_and_outputs_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp, "out")
            calls = []
            with mock.patch.dict(os.environ, {**ENV, "GITHUB_OUTPUT": str(out)}, clear=True), \
                    mock.patch.object(status, "github", side_effect=lambda *a, **k: calls.append(a) or {"id": 42}):
                status.main("start")
            self.assertIn("status-comment-id=42", out.read_text())
        body = calls[0][3]["body"]
        self.assertIn("> [!NOTE]", body)
        self.assertIn('alt="In progress"', body)
        self.assertIn("in progress", body)
        self.assertIn("Reviewing", body)
        self.assertTrue(body.startswith(status.STATUS_MARKER))

    def test_fail_edits_note_with_reason_and_reacts(self):
        path = self.events({"type": "error", "message": "model not found"})
        calls = []
        env = {**ENV, "STATUS_COMMENT_ID": "42", "TRIGGER_COMMENT_ID": "7", "EVENTS_FILE": path}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(status, "github", side_effect=lambda *a, **k: calls.append(a)):
            status.main("fail")
        self.assertEqual(calls[0][:2], ("PATCH", "/repos/o/r/issues/comments/42"))
        body = calls[0][3]["body"]
        self.assertIn("> [!CAUTION]", body)
        self.assertIn('alt="Failed"> **Codex review failed**', body)
        # The reason stays a code block, inside the alert: every line is quoted.
        self.assertIn("> ```\n> model not found\n> ```", body)
        self.assertEqual(calls[1][1], "/repos/o/r/issues/comments/7/reactions")
        self.assertEqual(calls[1][3], {"content": "confused"})

    def test_skip_posts_a_size_note(self):
        calls = []
        env = {**ENV, "CHANGED_LINES": "1200", "MAX_CHANGED_LINES": "500"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(status, "github", side_effect=lambda *a, **k: calls.append(a)):
            status.main("skip")
        self.assertEqual(calls[0][:2], ("POST", "/repos/o/r/issues/5/comments"))
        body = calls[0][3]["body"]
        self.assertIn("> [!WARNING]", body)
        self.assertIn('alt="Skipped"> **Codex review skipped**', body)
        self.assertIn("> PR too large to review (1200 changed lines, limit 500).", body)
        self.assertEqual(len(calls), 1)  # nothing to delete, no reaction

    def test_status_notes_are_text_only_without_icons(self):
        calls = []
        env = {**ENV, "CHANGED_LINES": "1200", "MAX_CHANGED_LINES": "500", "ICONS": "false"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(status, "github", side_effect=lambda *a, **k: calls.append(a)):
            status.main("skip")
        body = calls[0][3]["body"]
        self.assertNotIn("<img", body)
        self.assertIn("> [!WARNING]\n> **Codex review skipped**", body)

    def test_done_deletes_note_and_reacts(self):
        calls = []
        with mock.patch.dict(os.environ, {**ENV, "STATUS_COMMENT_ID": "42", "TRIGGER_COMMENT_ID": "7"}, clear=True), \
                mock.patch.object(status, "github", side_effect=lambda *a, **k: calls.append(a)):
            status.main("done")
        self.assertEqual(calls[0][:2], ("DELETE", "/repos/o/r/issues/comments/42"))
        self.assertEqual(calls[1][3], {"content": "rocket"})

    def test_done_without_a_requesting_comment_only_removes_the_note(self):
        """How a cancelled run clears up: the note goes, the request is not
        answered, so it gets no reaction."""
        calls = []
        with mock.patch.dict(os.environ, {**ENV, "STATUS_COMMENT_ID": "42", "TRIGGER_COMMENT_ID": ""}, clear=True), \
                mock.patch.object(status, "github", side_effect=lambda *a, **k: calls.append(a)):
            status.main("done")
        self.assertEqual([call[:2] for call in calls], [("DELETE", "/repos/o/r/issues/comments/42")])


if __name__ == "__main__":
    unittest.main()

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

    def test_single_finding_heading(self):
        # With one finding Codex writes "Review comment:" instead of "Full review comments:".
        text = f"One issue.\n\nReview comment:\n\n- [P2] Guard empty input \u2014 {WORKSPACE}/a.py:4\n  Explain.\n"
        summary, findings = pr.parse_review(text, WORKSPACE)
        self.assertEqual(summary, "One issue.")
        self.assertEqual([(f["priority"], f["path"], f["start"]) for f in findings], [(2, "a.py", 4)])

    def test_no_findings(self):
        summary, findings = pr.parse_review("No issues found in the changes.", WORKSPACE)
        self.assertEqual((summary, findings), ("No issues found in the changes.", []))

    def test_multiline_body(self):
        text = f"S.\n\nFull review comments:\n\n- [P0] Crash \u2014 {WORKSPACE}/a/b.py:3-7\n  One.\n  Two.\n"
        _, findings = pr.parse_review(text, WORKSPACE)
        self.assertEqual((findings[0]["path"], findings[0]["body"]), ("a/b.py", "One.\nTwo."))

    def test_suggestion_blocks_survive_parsing(self):
        # Captured from `codex exec review` 0.159.1 with the suggestions guidance on.
        _, findings = pr.parse_review((FIXTURES / "review-suggestions.md").read_text(), WORKSPACE)
        self.assertEqual([bool(f["suggestion"]) for f in findings], [True, False, True])
        # The replacement keeps the source file's own two-space indentation.
        self.assertEqual(findings[0]["suggestion"], "  const discounted = subtotal - subtotal * (percent / 100);")
        self.assertNotIn("suggestion", findings[0]["body"])
        self.assertTrue(findings[0]["body"].endswith("before applying it."))

    def test_body_indentation_is_only_dedented_by_its_common_indent(self):
        text = (
            f"S.\n\nFull review comments:\n\n- [P0] Crash \u2014 a/b.py:3-4\n"
            "  Because:\n  ```suggestion\n      deeply = 1\n  ```\n"
        )
        _, findings = pr.parse_review(text, WORKSPACE)
        self.assertEqual(findings[0]["suggestion"], "    deeply = 1")

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

    def test_verdict_alert_type_follows_the_worst_priority(self):
        def kind(*priorities):
            return pr.verdict([dict(self.findings[0], priority=p) for p in priorities]).splitlines()[0]
        self.assertEqual(kind(), "> [!TIP]")  # clean
        self.assertEqual(kind(0), "> [!CAUTION]")
        self.assertEqual(kind(1), "> [!CAUTION]")
        self.assertEqual(kind(3, 1), "> [!CAUTION]")
        self.assertEqual(kind(2), "> [!WARNING]")
        self.assertEqual(kind(3, 2), "> [!WARNING]")
        self.assertEqual(kind(3), "> [!NOTE]")

    def test_verdict_counts_resolved(self):
        self.assertIn("**No issues found.** · 3 resolved", pr.verdict([], 3))
        self.assertIn("(1 P1, 1 P2) · 1 resolved", pr.verdict(self.findings, 1))

    def test_alert_prefixes_every_line_and_keeps_blank_ones(self):
        self.assertEqual(pr.alert("NOTE", "one\n\ntwo"), "> [!NOTE]\n> one\n>\n> two")

    def test_body_lists_every_issue_and_collapses_non_inline(self):
        body = pr.body_markdown("Summary.", self.findings, ctx(), {id(self.findings[0])})
        self.assertTrue(body.startswith(pr.MARKER))
        self.assertIn("> [!CAUTION]", body)
        self.assertIn("|  | Priority | Issue | Location | Where |", body)
        self.assertIn("/priority-p1.svg\" width=\"16\" height=\"16\" alt=\"High\"> | P1 | Convert the percentage", body)
        self.assertIn('alt="Commented inline on the diff"> Inline |', body)
        self.assertIn('alt="Reported in the review body"> Below |', body)
        details = body.split("**Details**", 1)[1]
        self.assertIn("<details>", details)
        self.assertIn("Handle zero units", details)
        self.assertNotIn("Convert the percentage", details)

    def test_text_only_mode_drops_the_icon_column_and_every_image(self):
        body = pr.body_markdown("Summary.", self.findings, ctx(ICONS="false"), {id(self.findings[0])})
        self.assertNotIn("<img", body)
        self.assertIn("| Priority | Issue | Location | Where |", body)
        self.assertIn("| P1 | Convert the percentage", body)
        self.assertIn("| Inline |", body)
        self.assertIn("| Below |", body)
        self.assertIn("> [!CAUTION]", body)

    def test_meta_and_cta_footer(self):
        body = pr.body_markdown("Summary.", [], ctx())
        self.assertIn(f"Reviewed [`{SHA[:7]}`](https://github.com/o/r/commit/{SHA}) against `main`", body)
        self.assertIn("`gpt-6.1-sol` via OpenAI API", body)
        self.assertIn("[View run](https://github.com/o/r/actions/runs/9)", body)
        self.assertIn("Comment `@gpt review` to re-run.", body)

    def test_inline_comment_links_back(self):
        text = pr.inline_comment(self.findings[0], ctx())
        self.assertIn('alt="High"> **P1 \u00b7 Convert the percentage', text)
        self.assertIn("#L15", text)
        self.assertTrue(text.rstrip().endswith(pr.MARKER))
        self.assertTrue(pr.inline_comment(self.findings[0], ctx(ICONS="false")).startswith("**P1 \u00b7 "))

    def test_details_summary_carries_the_priority_icon(self):
        summary = pr.details(self.findings[1], ctx()).splitlines()[1]
        self.assertIn("/priority-p2.svg", summary)
        self.assertIn('alt="Medium"> <b>P2</b>', summary)
        self.assertTrue(pr.details(self.findings[1], ctx(ICONS="false")).splitlines()[1]
                        .startswith("<summary><b>P2</b>"))

    def test_html_in_titles_is_escaped_in_details(self):
        f = dict(self.findings[0], title="Use <T> & friends")
        self.assertIn("Use &lt;T&gt; &amp; friends", pr.details(f, ctx()))

    def test_pipe_in_title_is_escaped(self):
        f = dict(self.findings[0], title="a | b")
        self.assertIn("a \\| b", pr.issues_table([f], ctx(), set()))


class SuggestionRenderTest(unittest.TestCase):
    def setUp(self):
        _, self.findings = pr.parse_review((FIXTURES / "review-suggestions.md").read_text(), WORKSPACE)
        self.fixed, self.plain = self.findings[0], self.findings[1]

    def test_exact_range_passes_the_block_through_to_github(self):
        text = pr.inline_comment(self.fixed, ctx(), exact_range=True)
        self.assertIn("```suggestion\n  const discounted", text)
        self.assertNotIn("**Suggested fix**", text)

    def test_mismatched_range_cannot_be_applied(self):
        text = pr.inline_comment(self.fixed, ctx(), exact_range=False)
        self.assertNotIn("```suggestion", text)
        self.assertIn("**Suggested fix** (replaces `e2e/pricing.ts:16`)", text)
        self.assertIn("  const discounted", text)

    def test_suggestions_turned_off_keeps_it_as_a_code_block(self):
        text = pr.inline_comment(self.fixed, ctx(), exact_range=True, native=False)
        self.assertNotIn("```suggestion", text)
        self.assertIn("**Suggested fix**", text)

    def test_findings_without_a_suggestion_are_unchanged(self):
        self.assertEqual(pr.suggestion_block(self.plain), "")
        self.assertNotIn("Suggested fix", pr.inline_comment(self.plain, ctx()))

    def test_details_never_carry_an_applicable_suggestion(self):
        text = pr.details(self.fixed, ctx())
        self.assertNotIn("```suggestion", text)
        self.assertIn("**Suggested fix**", text)
        self.assertNotIn("(replaces", text)  # the location is already in the block

    def test_table_marks_findings_that_carry_a_fix(self):
        rows = pr.issues_table(self.findings, ctx(), set()).splitlines()[2:]
        self.assertEqual(['alt="Suggested fix"' in row for row in rows], [True, False, True])
        rows = pr.issues_table(self.findings, ctx(ICONS="false"), set()).splitlines()[2:]
        self.assertEqual(["<sub>(suggested fix)</sub>" in row for row in rows], [True, False, True])

    def test_review_anchors_a_range_before_passing_a_suggestion_through(self):
        """A range finding only gets a native suggestion when both ends are in the diff."""
        wide = dict(self.fixed, start=14, end=16)
        posted = {}
        with mock.patch.object(pr, "commentable_lines", return_value={"e2e/pricing.ts": {14, 15, 16}}), \
                mock.patch.object(pr, "github", side_effect=lambda *a, **k: posted.update(a[3]) or {}):
            pr.post_review("o/r", "1", "t", "S.", [wide], ctx())
        self.assertEqual(posted["comments"][0]["start_line"], 14)
        self.assertIn("```suggestion", posted["comments"][0]["body"])

        posted.clear()
        with mock.patch.object(pr, "commentable_lines", return_value={"e2e/pricing.ts": {16}}), \
                mock.patch.object(pr, "github", side_effect=lambda *a, **k: posted.update(a[3]) or {}):
            pr.post_review("o/r", "1", "t", "S.", [wide], ctx())
        self.assertNotIn("start_line", posted["comments"][0])
        self.assertNotIn("```suggestion", posted["comments"][0]["body"])
        self.assertIn("replaces `e2e/pricing.ts:14-16`", posted["comments"][0]["body"])


class HidePreviousTest(unittest.TestCase):
    """Earlier Codex comments are collapsed from ids read once for the whole run."""

    def calls(self, comments=(), review_comments=()):
        log = []

        def api(method, path, token, payload=None, url=None):
            log.append((method, path, payload))
            if "page=2" in path:
                return []
            if "/issues/1/comments" in path:
                return list(comments)
            if "/pulls/1/comments" in path:
                return list(review_comments)
            return {}

        return api, log

    def test_prefetched_ids_need_no_listing(self):
        api, log = self.calls()
        with mock.patch.object(pr, "github", side_effect=api):
            hidden = pr.hide_previous("o/r", "1", "t", "", [{"node_id": "IC_1", "id": 7},
                                                            {"node_id": "RC_1", "id": 8}])
        self.assertEqual(hidden, 2)
        self.assertEqual([path for _, path, _ in log], ["", ""])  # two GraphQL mutations only

    def test_the_progress_note_is_never_collapsed(self):
        api, _ = self.calls()
        with mock.patch.object(pr, "github", side_effect=api):
            hidden = pr.hide_previous("o/r", "1", "t", "7", [{"node_id": "IC_1", "id": 7},
                                                             {"node_id": "RC_1", "id": 8}])
        self.assertEqual(hidden, 1)

    def test_without_them_it_lists_both_kinds_of_comment(self):
        api, log = self.calls(comments=[{"node_id": "IC_1", "id": 7, "body": pr.MARKER + "\nreview"},
                                        {"node_id": "IC_2", "id": 9, "body": "someone else"}],
                              review_comments=[{"node_id": "RC_1", "id": 8, "body": "x\n" + pr.MARKER}])
        with mock.patch.object(pr, "github", side_effect=api):
            hidden = pr.hide_previous("o/r", "1", "t")
        self.assertEqual(hidden, 2)
        self.assertEqual(len([p for _, p, _ in log if "/comments" in p]), 2)


class PathFilterMainTest(unittest.TestCase):
    """include-paths and exclude-paths drop findings after parsing."""

    def run_main(self, **env):
        return run_publish((FIXTURES / "review-prefixed-relative.md").read_text(), **env)

    def test_exclude_drops_findings_and_notes_it(self):
        _, outputs, findings, summary = self.run_main(EXCLUDE_PATHS="**/pricing.ts")
        self.assertEqual((outputs["findings-count"], outputs["path-filtered-count"]), ("0", "3"))
        self.assertEqual(findings, [])
        self.assertIn("3 findings outside the reviewed paths not shown", summary)

    def test_include_keeps_only_matching_findings(self):
        _, outputs, _, _ = self.run_main(INCLUDE_PATHS="src/**")
        self.assertEqual((outputs["findings-count"], outputs["path-filtered-count"]), ("3", "0"))
        _, outputs, _, _ = self.run_main(INCLUDE_PATHS="lib/**")
        self.assertEqual((outputs["findings-count"], outputs["path-filtered-count"]), ("0", "3"))

    def test_no_filter_keeps_everything(self):
        _, outputs, _, summary = self.run_main()
        self.assertEqual((outputs["findings-count"], outputs["path-filtered-count"]), ("3", "0"))
        self.assertNotIn("outside the reviewed paths", summary)

    def test_size_note_reaches_the_footer(self):
        _, _, _, summary = self.run_main(SIZE_NOTE="Large PR: 900 changed lines, over the 500 line limit.")
        self.assertIn("Large PR: 900 changed lines", summary)


def run_publish(review_text, **env):
    """Run publish_review.main() on one review, returning (code, outputs, findings, summary)."""
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


class MainTest(unittest.TestCase):
    def run_main(self, review_text, **env):
        return run_publish(review_text, **env)

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


class MergeVerdictTest(unittest.TestCase):
    """The verdict, health score and confidence in the body, the outputs and the gates."""

    def setUp(self):
        self.text = (FIXTURES / "review-two-findings.md").read_text()
        self.clean = "No issues found in the changes."

    def test_the_verdict_leads_the_body_with_the_score_and_confidence(self):
        _, outputs, _, summary = run_publish(self.text)
        self.assertIn("**Changes requested** · Health 75/100 · Confidence: high", summary)
        self.assertIn("2 issues to address (1 P1, 1 P2)", summary)
        self.assertIn("<summary><b>Why this score</b></summary>", summary)
        self.assertIn("| 1 P1 finding | -20 |", summary)
        self.assertEqual((outputs["verdict"], outputs["health-score"], outputs["confidence"]),
                         ("changes-requested", "75", "high"))
        self.assertEqual(outputs["health-trend"], "")

    def test_a_clean_review_is_ready_to_merge(self):
        _, outputs, _, summary = run_publish(self.clean)
        self.assertIn("> [!TIP]", summary)
        self.assertIn("**Ready to merge** · Health 100/100", summary)
        self.assertEqual((outputs["verdict"], outputs["health-score"]), ("ready", "100"))

    def test_a_low_confidence_clean_review_does_not_claim_mergeable(self):
        _, outputs, _, summary = run_publish(self.clean, CHANGED_LINES="900", MAX_CHANGED_LINES="500")
        self.assertIn("**Needs a full review**", summary)
        self.assertIn("Confidence: low (one pass over 900 changed lines, over the 500 line limit)", summary)
        self.assertIn("did not cover the whole pull request", summary)
        self.assertEqual((outputs["verdict"], outputs["confidence"]), ("changes-requested", "low"))

    def test_a_coverage_report_sets_the_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            coverage = pathlib.Path(tmp, "coverage.json")
            coverage.write_text(json.dumps({"mode": "full", "complete": False, "files_total": 224,
                                            "files_inspected": 38, "uncovered": [], "shards": 6,
                                            "passes": 6}))
            _, outputs, _, summary = run_publish(self.clean, COVERAGE_FILE=str(coverage),
                                                 CHANGED_LINES="10")
        self.assertEqual(outputs["confidence"], "low")
        self.assertIn("partial review, 38/224 files inspected", summary)

    def test_the_trend_comes_from_the_previous_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp, "state.json")
            state.write_text(json.dumps({"previous": {"v": 1, "sha": SHA, "findings": [],
                                                      "hs": 55, "vd": "blocked"}}))
            _, outputs, _, summary = run_publish(self.text, STATE_FILE=str(state))
        self.assertEqual(outputs["health-trend"], "+20")
        self.assertIn("Health 55 -> 75 (+20 since last review)", summary)

    def test_the_state_marker_carries_the_verdict_and_the_score(self):
        calls = []
        with mock.patch.object(pr, "github", side_effect=lambda *a, **k: calls.append(a) or []):
            run_publish(self.text, POST_MODE="comment", GH_TOKEN="t", LABELS="false")
        body = [c[3]["body"] for c in calls if c[0] == "POST" and c[1].endswith("/issues/1/comments")][0]
        state = json.loads(pr.history.STATE_RE.search(body).group(1))
        self.assertEqual((state["vd"], state["hs"]), ("changes-requested", 75))

    def test_the_health_file_reaches_the_check_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            health_file = pathlib.Path(tmp, "health.json")
            run_publish(self.text, HEALTH_FILE=str(health_file))
            saved = json.loads(health_file.read_text())
        self.assertEqual((saved["verdict"], saved["score"], saved["confidence"]),
                         ("changes-requested", 75, "high"))
        self.assertIn(["1 P1 finding", 20], saved["deductions"])

    def test_fail_on_verdict_gates_the_step(self):
        self.assertEqual(run_publish(self.text, FAIL_ON_VERDICT="changes-requested")[0], 1)
        self.assertEqual(run_publish(self.text, FAIL_ON_VERDICT="blocked")[0], 0)
        self.assertEqual(run_publish(self.clean, FAIL_ON_VERDICT="nits")[0], 0)
        self.assertEqual(run_publish(self.clean, FAIL_ON_VERDICT="ready")[0], 1)
        self.assertEqual(run_publish(self.text)[0], 0)

    def test_both_gates_still_work_together(self):
        self.assertEqual(run_publish(self.text, FAIL_ON_PRIORITY="P1", FAIL_ON_VERDICT="blocked")[0], 1)
        self.assertEqual(run_publish(self.text, FAIL_ON_PRIORITY="P0", FAIL_ON_VERDICT="nits")[0], 1)

    def test_an_invalid_gate_is_refused(self):
        with self.assertRaises(SystemExit):
            run_publish(self.text, FAIL_ON_VERDICT="P1")


class LabelTest(unittest.TestCase):
    """The pull request is labelled with the verdict whenever a review is posted."""

    def run_main(self, review_text, **env):
        calls = []

        def api(method, path, token, payload=None, url=None):
            calls.append((method, path, payload))
            return [] if path.endswith("/files") or "page=" in path else {"id": 1}

        with mock.patch.object(pr, "github", side_effect=api):
            code, outputs, _, _ = run_publish(review_text, POST_MODE="comment", GH_TOKEN="t", **env)
        return code, outputs, calls

    def test_the_verdict_label_goes_on_by_default(self):
        _, outputs, calls = self.run_main((FIXTURES / "review-two-findings.md").read_text())
        self.assertEqual(outputs["label"], "codex: changes-requested")
        self.assertIn(("POST", "/repos/o/r/labels",
                       {"name": "codex: changes-requested", "color": "d93f0b",
                        "description": "Codex review: changes requested"}), calls)
        self.assertIn(("POST", "/repos/o/r/issues/1/labels", {"labels": ["codex: changes-requested"]}), calls)

    def test_labels_false_touches_nothing(self):
        _, outputs, calls = self.run_main("No issues found.", LABELS="false")
        self.assertNotIn("label", outputs)
        self.assertEqual([c for c in calls if "labels" in c[1]], [])

    def test_post_mode_none_posts_no_label_either(self):
        _, outputs, _, _ = run_publish("No issues found.", POST_MODE="none")
        self.assertNotIn("label", outputs)


class ReviewEventTest(unittest.TestCase):
    """review-event, and what happens when GitHub will not accept it."""

    def post(self, event, refuse=0, **env):
        posted = []

        def api(method, path, token, payload=None, url=None):
            if method == "POST" and path.endswith("/reviews"):
                posted.append(payload)
                if len(posted) <= refuse:
                    raise urllib.error.HTTPError(
                        "u", 422, "Unprocessable", {},
                        io.BytesIO(b'{"message": "Review cannot be requested from the author"}'))
            files = "/pulls/1/files" in path and "page=1" in path
            return [{"filename": "src/pricing.ts", "patch": "@@ -10,3 +10,16 @@\n" + "+a\n" * 13}] if files else []

        with mock.patch.object(pr, "github", side_effect=api):
            run_publish((FIXTURES / "review-two-findings.md").read_text(),
                        POST_MODE="review", GH_TOKEN="t", LABELS="false", REVIEW_EVENT=event, **env)
        return posted

    def test_the_default_is_a_plain_comment(self):
        self.assertEqual([p["event"] for p in self.post("")], ["COMMENT"])

    def test_auto_requests_changes_on_a_changes_requested_verdict(self):
        posted = self.post("auto")
        self.assertEqual([p["event"] for p in posted], ["REQUEST_CHANGES"])
        self.assertTrue(posted[0]["comments"])

    def test_a_refused_event_falls_back_to_a_comment_with_the_inline_comments_kept(self):
        posted = self.post("REQUEST_CHANGES", refuse=1)
        self.assertEqual([p["event"] for p in posted], ["REQUEST_CHANGES", "COMMENT"])
        self.assertEqual(len(posted[0]["comments"]), len(posted[1]["comments"]))
        self.assertTrue(posted[1]["comments"])

    def test_refusing_twice_drops_the_inline_comments_as_well(self):
        posted = self.post("auto", refuse=2)
        self.assertEqual([p["event"] for p in posted], ["REQUEST_CHANGES", "COMMENT", "COMMENT"])
        self.assertEqual(posted[2]["comments"], [])

    def test_an_invalid_event_is_refused(self):
        with self.assertRaises(SystemExit):
            self.post("APPROVE")


if __name__ == "__main__":
    unittest.main()


class RetryTest(unittest.TestCase):
    """The GitHub helper retries transient failures, but never repeats a POST that may have landed."""

    def call(self, method, failures, url=None):
        import http.client
        import urllib.error
        attempts = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        def urlopen(request, timeout=None):
            attempts.append(request.get_method())
            if len(attempts) <= len(failures):
                failure = failures[len(attempts) - 1]
                if isinstance(failure, int):
                    raise urllib.error.HTTPError(request.full_url, failure, "x", {}, None)
                raise failure
            return Response()

        with mock.patch.object(pr.urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(pr.time, "sleep"):
            result = pr.github(method, "/x", "t", {"a": 1} if method != "GET" else None, url=url)
        return result, len(attempts)

    def test_get_retries_disconnects_and_5xx(self):
        import http.client
        result, attempts = self.call("GET", [http.client.RemoteDisconnected("gone"), 502])
        self.assertEqual((result, attempts), ({"ok": True}, 3))

    def test_post_retries_only_unprocessed_statuses(self):
        import http.client
        self.assertEqual(self.call("POST", [503])[1], 2)
        with self.assertRaises(http.client.RemoteDisconnected):
            self.call("POST", [http.client.RemoteDisconnected("gone")])
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self.call("POST", [502])

    def test_graphql_post_is_treated_as_idempotent(self):
        import http.client
        _, attempts = self.call("POST", [http.client.RemoteDisconnected("gone")], url="https://api.github.com/graphql")
        self.assertEqual(attempts, 2)

    def test_gives_up_after_the_last_delay(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self.call("GET", [502, 502, 502, 502])

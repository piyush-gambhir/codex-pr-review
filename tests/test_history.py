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

SHA = "0123456789abcdef0123456789abcdef01234567"
OLD_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def finding(title, path="src/pricing.ts", priority=1, start=15, end=None):
    item = {"priority": priority, "title": title, "path": path, "start": start,
            "end": end or start, "body": "Because."}
    item["fingerprint"] = history.fingerprint(item["path"], item["title"])
    return item


class FingerprintTest(unittest.TestCase):
    def test_survives_line_shifts_and_word_order(self):
        base = history.fingerprint("src/pricing.ts", "Convert the percentage before applying it")
        self.assertEqual(base, history.fingerprint("src/pricing.ts", "Convert the percentage before applying it"))
        # Word order, casing, punctuation and stopwords do not matter.
        self.assertEqual(base, history.fingerprint("src/pricing.ts", "applying, before converting"[:0] + "Percentage convert before applying it"))
        self.assertEqual(base, history.fingerprint("SRC/Pricing.ts", "Convert percentage before applying"))

    def test_numbers_are_ignored(self):
        self.assertEqual(
            history.fingerprint("a.ts", "Guard line 15 against zero units"),
            history.fingerprint("a.ts", "Guard line 402 against zero units"),
        )

    def test_different_path_or_subject_differs(self):
        one = history.fingerprint("a.ts", "Handle zero units")
        self.assertNotEqual(one, history.fingerprint("b.ts", "Handle zero units"))
        self.assertNotEqual(one, history.fingerprint("a.ts", "Validate the coupon code"))

    def test_similarity(self):
        left = history.title_tokens("Convert the percentage before applying it")
        self.assertEqual(history.similarity(left, left), 1.0)
        self.assertGreater(history.similarity(left, history.title_tokens("Convert percentage before applying")), 0.5)
        self.assertEqual(history.similarity(left, history.title_tokens("Handle zero units")), 0.0)


class StateTest(unittest.TestCase):
    def test_round_trip(self):
        findings = [finding("Convert the percentage"), finding("Handle zero units", priority=2, start=22)]
        marker = history.state_marker(SHA, findings)
        self.assertTrue(marker.startswith(history.STATE_PREFIX))
        body = f"## Codex review\n\nsome text\n\n{marker}"
        state = history.parse_state(body)
        self.assertEqual(state["sha"], SHA)
        self.assertEqual([e["fp"] for e in state["findings"]], [f["fingerprint"] for f in findings])
        self.assertEqual(state["findings"][1], {"fp": findings[1]["fingerprint"], "pri": 2,
                                                "path": "src/pricing.ts", "line": 22, "title": "Handle zero units"})
        self.assertEqual(history.strip_state(body).strip(), "## Codex review\n\nsome text")

    def test_carried_entries_stay_in_the_state(self):
        carried = [history.entry(finding("Old one", path="src/other.ts"))]
        state = history.parse_state(history.state_marker(SHA, [finding("New one")], carried))
        self.assertEqual(len(state["findings"]), 2)
        self.assertIn("Old one", [e["title"] for e in state["findings"]])

    def test_angle_brackets_and_long_titles_cannot_break_the_marker(self):
        state = history.parse_state(history.state_marker(SHA, [finding("Bad <!-- --> " + "x" * 200)]))
        self.assertEqual(len(state["findings"]), 1)
        self.assertLessEqual(len(state["findings"][0]["title"]), history.TITLE_LIMIT)

    def test_missing_or_broken_state(self):
        self.assertIsNone(history.parse_state("no marker here"))
        self.assertIsNone(history.parse_state(history.STATE_PREFIX + "{nope} -->"))

    def test_finding_marker(self):
        marker = history.finding_marker("abc123")
        self.assertEqual(history.FINDING_RE.search(f"body\n{marker}").group(1), "abc123")
        self.assertEqual(history.strip_state(marker), "")


class LatestStateTest(unittest.TestCase):
    def calls(self, comments, reviews):
        def call(method, path, token, payload=None, url=None):
            if "/issues/1/comments" in path:
                return comments if "page=1" in path else []
            if "/pulls/1/reviews" in path:
                return reviews if "page=1" in path else []
            return []
        return call

    def test_picks_the_most_recent_state(self):
        old = history.state_marker(OLD_SHA, [finding("Old finding")])
        new = history.state_marker(SHA, [finding("New finding")])
        call = self.calls(
            [{"created_at": "2026-01-01T00:00:00Z", "body": f"note {old}"}, {"created_at": "2026-01-02T00:00:00Z", "body": "no state"}],
            [{"submitted_at": "2026-01-03T00:00:00Z", "body": f"review {new}"}],
        )
        self.assertEqual(history.latest_state("o/r", "1", "t", call)["sha"], SHA)

    def test_no_previous_state(self):
        self.assertEqual(history.latest_state("o/r", "1", "t", self.calls([{"created_at": "x", "body": "hi"}], [])), {})

    def test_the_state_carries_a_link_to_where_it_was_found(self):
        marker = history.state_marker(SHA, [finding("New finding")])
        call = self.calls([{"created_at": "2026-01-01T00:00:00Z", "body": marker,
                            "html_url": "https://github.com/o/r/pull/1#issuecomment-7"}], [])
        self.assertEqual(history.latest_state("o/r", "1", "t", call)["url"],
                         "https://github.com/o/r/pull/1#issuecomment-7")

    def test_bodies_already_in_hand_are_not_listed_again(self):
        def refuse(*args, **kwargs):
            raise AssertionError("latest_state must not call the API when it is given the bodies")

        items = [{"created_at": "2026-01-01T00:00:00Z", "body": history.state_marker(OLD_SHA, [])},
                 {"submitted_at": "2026-01-02T00:00:00Z", "body": history.state_marker(SHA, [])}]
        self.assertEqual(history.latest_state("o/r", "1", "t", refuse, items)["sha"], SHA)
        # An empty conversation is an answer too, not a reason to go and look.
        self.assertEqual(history.latest_state("o/r", "1", "t", refuse, []), {})


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.previous = [
            history.entry(finding("Convert the percentage before applying it")),
            history.entry(finding("Handle zero units", priority=2, start=22)),
            history.entry(finding("Validate the coupon code", path="src/coupons.ts", start=8)),
        ]

    def test_full_review_resolves_what_is_gone(self):
        current = [finding("Handle zero units", priority=2, start=30)]
        resolved, carried, still_open = history.classify(self.previous, current)
        self.assertEqual([e["title"] for e in resolved],
                         ["Convert the percentage before applying it", "Validate the coupon code"])
        self.assertEqual(carried, [])
        self.assertEqual(still_open, {current[0]["fingerprint"]})

    def test_reworded_finding_counts_as_still_open(self):
        current = [finding("Convert percentage before applying", start=40)]
        resolved, _, still_open = history.classify(self.previous, current)
        self.assertNotIn("Convert the percentage before applying it", [e["title"] for e in resolved])
        self.assertEqual(still_open, {current[0]["fingerprint"]})

    def test_incremental_carries_untouched_files_forward(self):
        # Only pricing.ts changed in the new commits, and one of its findings is gone.
        current = [finding("Handle zero units", priority=2, start=22)]
        resolved, carried, still_open = history.classify(self.previous, current, ["src/pricing.ts"])
        self.assertEqual([e["title"] for e in resolved], ["Convert the percentage before applying it"])
        self.assertEqual([e["title"] for e in carried], ["Validate the coupon code"])
        self.assertEqual(still_open, {current[0]["fingerprint"]})

    def test_incremental_with_no_changed_files_resolves_nothing(self):
        resolved, carried, _ = history.classify(self.previous, [], [])
        self.assertEqual(resolved, [])
        self.assertEqual(len(carried), 3)

    def test_no_previous_state(self):
        self.assertEqual(history.classify([], [finding("New")]), ([], [], set()))


class ThreadTest(unittest.TestCase):
    def threads(self, nodes):
        self.sent = []

        def call(method, path, token, payload=None, url=None):
            self.sent.append(payload)
            if "reviewThreads" in payload["query"]:
                return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}}
            return {"data": {"resolveReviewThread": {"clientMutationId": None}}}
        return call

    def node(self, node_id, body, resolved=False):
        return {"id": node_id, "isResolved": resolved, "comments": {"nodes": [{"body": body}]}}

    def test_matches_threads_on_the_finding_marker(self):
        fixed, open_fp = "aaa111", "bbb222"
        call = self.threads([
            self.node("T1", f"inline\n{history.finding_marker(fixed)}\n{pr.MARKER}"),
            self.node("T2", f"inline\n{history.finding_marker(open_fp)}\n{pr.MARKER}"),
            self.node("T3", f"already done\n{history.finding_marker(fixed)}", resolved=True),
            self.node("T4", "a human comment"),
        ])
        self.assertEqual(history.resolve_threads("o/r", "1", "t", {fixed}, call, "https://g"), 1)
        mutations = [p["variables"]["id"] for p in self.sent if "resolveReviewThread" in p["query"]]
        self.assertEqual(mutations, ["T1"])

    def test_nothing_to_resolve_makes_no_calls(self):
        call = self.threads([])
        self.assertEqual(history.resolve_threads("o/r", "1", "t", set(), call, "https://g"), 0)
        self.assertEqual(self.sent, [])

    def test_api_failure_is_not_fatal(self):
        def call(*args, **kwargs):
            raise OSError("boom")
        self.assertEqual(history.resolve_threads("o/r", "1", "t", {"a"}, call, "https://g"), 0)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.state = history.state_marker(OLD_SHA, [finding("Convert the percentage")])
        self.call = lambda method, path, token, payload=None, url=None: (
            [{"created_at": "2026-01-01T00:00:00Z", "body": self.state}] if "/issues/1/comments" in path and "page=1" in path else []
        )
        self.env = {"GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1", "HEAD_SHA": SHA, "BASE_REF": "origin/main"}

    def test_full_review_by_default(self):
        result = history.plan(self.env, self.call)
        self.assertEqual((result["incremental"], result["base"], result["note"]), (False, "origin/main", ""))
        self.assertEqual(result["previous"]["sha"], OLD_SHA)

    def test_incremental_uses_the_previous_sha_as_base(self):
        with mock.patch.object(history, "git", side_effect=[(0, ""), (0, ""), (0, "src/pricing.ts\nREADME.md")]):
            result = history.plan({**self.env, "INCREMENTAL": "true"}, self.call)
        self.assertEqual((result["incremental"], result["base"]), (True, OLD_SHA))
        self.assertEqual(result["changed-files"], ["src/pricing.ts", "README.md"])
        self.assertEqual(result["note"], f"Incremental: `{OLD_SHA[:7]}`..`{SHA[:7]}`")

    def test_force_push_falls_back_to_a_full_review(self):
        with mock.patch.object(history, "git", side_effect=[(0, ""), (1, "")]):
            result = history.plan({**self.env, "INCREMENTAL": "true"}, self.call)
        self.assertFalse(result["incremental"])
        self.assertEqual(result["base"], "origin/main")
        self.assertIn("no longer in this branch's history", result["note"])

    def test_unknown_previous_commit_falls_back(self):
        with mock.patch.object(history, "git", side_effect=[(128, "")]):
            self.assertFalse(history.plan({**self.env, "INCREMENTAL": "true"}, self.call)["incremental"])

    def test_no_new_commits(self):
        self.state = history.state_marker(SHA, [finding("Convert the percentage")])
        result = history.plan({**self.env, "INCREMENTAL": "true"}, self.call)
        self.assertFalse(result["incremental"])
        self.assertIn("no new commits", result["note"])

    def test_plan_file_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file, out = pathlib.Path(tmp, "state.json"), pathlib.Path(tmp, "out")
            env = {**self.env, "STATE_FILE": str(state_file), "GITHUB_OUTPUT": str(out)}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(pr, "github", self.call):
                self.assertEqual(history.main("plan"), 0)
            outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
            self.assertEqual(outputs, {"review-base": "origin/main", "incremental": "false",
                                       "previous-sha": OLD_SHA, "note": ""})
            self.assertEqual(history.load_plan(str(state_file))["previous"]["sha"], OLD_SHA)
        self.assertEqual(history.load_plan(""), {})
        self.assertEqual(history.load_plan("/nonexistent/plan.json"), {})

    def test_the_plan_carries_what_publishing_needs(self):
        """A shared read puts the comments to collapse and the threads to resolve
        in the plan, so publish_review.py does not list the PR again."""
        bundle = {"items": [{"created_at": "2026-01-01T00:00:00Z", "body": self.state}],
                  "hide": [{"node_id": "IC_0", "id": 100}], "threads": [["PRRT_1", "abc123456789"]]}
        with tempfile.TemporaryDirectory() as tmp:
            state_file, out = pathlib.Path(tmp, "state.json"), pathlib.Path(tmp, "out")
            env = {**self.env, "STATE_FILE": str(state_file), "GITHUB_OUTPUT": str(out)}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(pr, "github", self.call), \
                    mock.patch("pr_state.fetch", return_value=bundle):
                self.assertEqual(history.main("plan"), 0)
            plan = history.load_plan(str(state_file))
        self.assertEqual(plan["previous"]["sha"], OLD_SHA)
        self.assertEqual(plan["hide"], bundle["hide"])
        self.assertEqual(plan["threads"], bundle["threads"])

    def test_without_a_shared_read_the_plan_asks_for_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file, out = pathlib.Path(tmp, "state.json"), pathlib.Path(tmp, "out")
            env = {**self.env, "STATE_FILE": str(state_file), "GITHUB_OUTPUT": str(out)}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(pr, "github", self.call), \
                    mock.patch("pr_state.fetch", return_value=None):
                self.assertEqual(history.main("plan"), 0)
            plan = history.load_plan(str(state_file))
        # None, not [], so every reader falls back to its own REST listing.
        self.assertNotIn("hide", plan)
        self.assertNotIn("threads", plan)

    def test_unknown_action(self):
        self.assertEqual(history.main("nope"), 1)


class RenderTest(unittest.TestCase):
    def ctx(self):
        return pr.Context({"GITHUB_REPOSITORY": "o/r", "HEAD_SHA": SHA, "PREVIOUS_SHA": OLD_SHA})

    def test_resolved_section_links_at_the_previous_commit(self):
        entries = [history.entry(finding("Convert the percentage"))]
        text = history.resolved_section(entries, [], self.ctx())
        self.assertIn("**Resolved since last review (1)**", text)
        self.assertIn('alt="Resolved"> ~~Convert the percentage~~', text)
        self.assertIn(f"https://github.com/o/r/blob/{OLD_SHA}/src/pricing.ts#L15", text)
        self.assertNotIn("<details>", text)

    def test_resolved_section_is_readable_without_icons(self):
        entries = [history.entry(finding("Convert the percentage"))]
        text = history.resolved_section(entries, [], pr.Context({"GITHUB_REPOSITORY": "o/r", "HEAD_SHA": SHA,
                                                                "ICONS": "false"}))
        self.assertIn("- ~~Convert the percentage~~", text)
        self.assertNotIn("<img", text)

    def test_long_lists_are_collapsed(self):
        entries = [history.entry(finding(f"Issue number {i}", start=i)) for i in range(5)]
        text = history.resolved_section(entries, [], self.ctx())
        self.assertIn("<details>", text)
        self.assertIn("<b>Resolved since last review (5)</b>", text)

    def test_carried_entries_are_listed_separately(self):
        carried = [history.entry(finding("Validate the coupon code", priority=2))]
        ctx = self.ctx()
        ctx.incremental = True
        text = history.resolved_section([], carried, ctx)
        self.assertIn("**Still open, not re-checked (1)**", text)
        self.assertIn("P2 Validate the coupon code", text)
        # A full review read the file; the finding is open because the file didn't change.
        ctx.incremental = False
        self.assertIn("**Still open, file unchanged (1)**", history.resolved_section([], carried, ctx))


class PublishIntegrationTest(unittest.TestCase):
    """The parts of publish_review.py that read and write the state."""

    def run_main(self, review_text, plan=None, **env):
        with tempfile.TemporaryDirectory() as tmp:
            review = pathlib.Path(tmp, "review.md")
            review.write_text(review_text)
            state_file = pathlib.Path(tmp, "state.json")
            if plan is not None:
                state_file.write_text(json.dumps(plan))
            out, summary = pathlib.Path(tmp, "out"), pathlib.Path(tmp, "summary")
            base = {"REVIEW_FILE": str(review), "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1",
                    "HEAD_SHA": SHA, "REVIEW_WORKSPACE": "/work", "POST_MODE": "none",
                    "STATE_FILE": str(state_file), "GITHUB_OUTPUT": str(out),
                    "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": tmp}
            self.calls = []
            with mock.patch.dict(os.environ, {**base, **env}, clear=True), \
                    mock.patch.object(pr, "github", side_effect=lambda *a, **k: self.calls.append((a, k)) or []):
                code = pr.main()
            outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
            return code, outputs, summary.read_text()

    def review_text(self, *titles):
        lines = ["Summary.", "", "Full review comments:", ""]
        for index, title in enumerate(titles):
            lines += [f"- [P1] {title} \u2014 /work/src/pricing.ts:{15 + index}", "  Because."]
        return "\n".join(lines) + "\n"

    def plan(self, *titles, **extra):
        entries = [history.entry(finding(t, start=15 + i)) for i, t in enumerate(titles)]
        return dict({"previous": {"v": 1, "sha": OLD_SHA, "findings": entries}}, **extra)

    def test_resolved_count_and_sections(self):
        plan = self.plan("Convert the percentage", "Handle zero units")
        code, outputs, summary = self.run_main(self.review_text("Handle zero units"), plan)
        self.assertEqual(code, 0)
        self.assertEqual(outputs["resolved-count"], "1")
        self.assertIn("1 resolved", summary)
        self.assertIn("Resolved since last review (1)", summary)
        self.assertIn('alt="Reported again"', summary)
        self.assertNotIn(history.STATE_PREFIX, summary)

    def test_still_open_is_a_word_without_icons(self):
        plan = self.plan("Convert the percentage", "Handle zero units")
        _, _, summary = self.run_main(self.review_text("Handle zero units"), plan, ICONS="false")
        self.assertIn("<sub>(still open)</sub>", summary)
        self.assertNotIn("<img", summary)

    def test_incremental_base_commit_is_shortened_in_the_meta_line(self):
        _, _, summary = self.run_main("No issues found.", BASE_REF=OLD_SHA)
        self.assertIn(f"against `{OLD_SHA[:7]}`", summary)

    def test_incremental_note_and_carried_findings(self):
        plan = self.plan("Convert the percentage", "Handle zero units")
        plan["previous"]["findings"][1]["path"] = "src/coupons.ts"
        plan["changed-files"] = ["src/pricing.ts"]
        plan["incremental"] = True
        _, outputs, summary = self.run_main("No issues found.", plan, INCREMENTAL_NOTE="Incremental: `a`..`b`")
        self.assertEqual(outputs["resolved-count"], "1")
        self.assertIn("Incremental: `a`..`b`", summary)
        self.assertIn("Still open, not re-checked (1)", summary)

    def test_state_marker_is_posted_and_carries_untouched_findings(self):
        plan = self.plan("Convert the percentage", "Handle zero units")
        plan["previous"]["findings"][1]["path"] = "src/coupons.ts"
        plan["changed-files"] = ["src/pricing.ts"]
        self.run_main(self.review_text("Convert the percentage"), plan,
                      POST_MODE="comment", GH_TOKEN="t", HIDE_PREVIOUS="false")
        body = [a[3]["body"] for a, _ in self.calls if a[0] == "POST"][0]
        state = history.parse_state(body)
        self.assertEqual(state["sha"], SHA)
        self.assertEqual(sorted(e["title"] for e in state["findings"]),
                         ["Convert the percentage", "Handle zero units"])

    def test_inline_comments_carry_their_fingerprint(self):
        text = pr.inline_comment(finding("Convert the percentage"), self.ctx())
        self.assertIn(history.finding_marker(history.fingerprint("src/pricing.ts", "Convert the percentage")), text)
        self.assertTrue(text.rstrip().endswith(pr.MARKER))

    def test_fixed_threads_are_resolved_after_posting(self):
        plan = self.plan("Convert the percentage")
        with mock.patch.object(history, "resolve_threads", return_value=1) as resolve:
            self.run_main("No issues found.", plan, POST_MODE="comment", GH_TOKEN="t", HIDE_PREVIOUS="false")
        self.assertEqual({e["fp"] for e in plan["previous"]["findings"]}, resolve.call_args[0][3])

    def test_resolve_fixed_threads_can_be_turned_off(self):
        with mock.patch.object(history, "resolve_threads") as resolve:
            self.run_main("No issues found.", self.plan("Convert the percentage"), POST_MODE="comment",
                          GH_TOKEN="t", HIDE_PREVIOUS="false", RESOLVE_FIXED_THREADS="false")
        resolve.assert_not_called()

    def test_without_previous_state_nothing_changes(self):
        _, outputs, summary = self.run_main(self.review_text("Convert the percentage"))
        self.assertEqual(outputs["resolved-count"], "0")
        self.assertNotIn("Resolved since last review", summary)
        self.assertNotIn("(still open)", summary)

    def ctx(self):
        return pr.Context({"GITHUB_REPOSITORY": "o/r", "HEAD_SHA": SHA})


if __name__ == "__main__":
    unittest.main()


class ChangedSinceTest(unittest.TestCase):
    """Only a finding whose file changed since the last review can count as fixed."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        run = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        pathlib.Path("a.py").write_text("a\n")
        pathlib.Path("b.py").write_text("b\n")
        run("add", "-A")
        run("commit", "-qm", "one")
        self.first = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        pathlib.Path("a.py").write_text("a2\n")
        run("commit", "-qam", "two")
        self.second = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_same_commit_changes_nothing(self):
        self.assertEqual(history.changed_since(self.second, self.second), [])

    def test_ancestor_lists_changed_files(self):
        self.assertEqual(history.changed_since(self.first, self.second), ["a.py"])

    def test_unknown_commit_is_not_comparable(self):
        self.assertIsNone(history.changed_since("0" * 40, self.second))
        self.assertIsNone(history.changed_since("", self.second))

    def test_unreported_finding_in_unchanged_file_is_not_resolved(self):
        previous = [
            {"fp": "fa", "path": "a.py", "title": "Fix a", "pri": 2, "line": 1},
            {"fp": "fb", "path": "b.py", "title": "Fix b", "pri": 2, "line": 1},
        ]
        resolved, carried, still_open = history.classify(previous, [], history.changed_since(self.first, self.second))
        self.assertEqual([i["path"] for i in resolved], ["a.py"])
        self.assertEqual([i["path"] for i in carried], ["b.py"])
        # An identical commit can't fix anything, whatever Codex reports this time.
        resolved, carried, _ = history.classify(previous, [], history.changed_since(self.second, self.second))
        self.assertEqual((resolved, len(carried)), ([], 2))

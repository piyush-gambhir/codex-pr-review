import io
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import verdict as v  # noqa: E402


def finding(priority=1, title="Convert the percentage", path="src/pricing.ts"):
    return {"priority": priority, "title": title, "path": path, "start": 1, "end": 1}


def carried(priority=1, title="Validate the coupon", path="src/coupons.ts"):
    return {"fp": "abc123", "pri": priority, "path": path, "line": 8, "title": title}


class VerdictRuleTest(unittest.TestCase):
    """The rule: nothing open is ready, P2/P3 nits, P1 asks, P0 blocks."""

    def test_the_worst_priority_open_decides(self):
        self.assertEqual(v.from_priorities([]), v.READY)
        self.assertEqual(v.from_priorities([3]), v.NITS)
        self.assertEqual(v.from_priorities([2, 3, 3]), v.NITS)
        self.assertEqual(v.from_priorities([1, 2, 3]), v.CHANGES)
        self.assertEqual(v.from_priorities([0, 3]), v.BLOCKED)

    def test_findings_and_carried_entries_are_both_open(self):
        self.assertEqual(v.open_priorities([finding(2)], [carried(1)]), [2, 1])
        self.assertEqual(v.open_priorities([], [carried(0)]), [0])
        self.assertEqual(v.open_priorities([], []), [])

    def test_a_carried_finding_alone_still_asks_for_changes(self):
        health = v.assess([], [carried(1)])
        self.assertEqual((health.verdict, health.score), (v.CHANGES, 80))

    def test_every_alert_type_and_headline(self):
        pairs = {v.READY: ("TIP", "Ready to merge"), v.NITS: ("NOTE", "Mergeable, with nits"),
                 v.CHANGES: ("WARNING", "Changes requested"), v.BLOCKED: ("CAUTION", "Do not merge")}
        for key, (kind, headline) in pairs.items():
            health = v.Health(key, 50, "high")
            self.assertEqual((health.alert, health.headline), (kind, headline))


class ConfidenceTest(unittest.TestCase):
    def test_a_complete_coverage_report_is_high(self):
        level, why = v.coverage_confidence({"complete": True, "files_total": 224, "files_inspected": 224})
        self.assertEqual(level, "high")
        self.assertIn("224/224 files inspected", why)

    def test_a_nearly_complete_report_is_medium(self):
        self.assertEqual(v.coverage_confidence(
            {"complete": False, "files_total": 100, "files_inspected": 90})[0], "medium")
        # `complete` with files left out is still only medium.
        self.assertEqual(v.coverage_confidence(
            {"complete": True, "files_total": 100, "files_inspected": 50})[0], "medium")

    def test_a_thin_report_is_low(self):
        level, why = v.coverage_confidence({"complete": False, "files_total": 224, "files_inspected": 38})
        self.assertEqual(level, "low")
        self.assertEqual(why, "partial review, 38/224 files inspected")

    def test_the_size_guard_decides_without_a_report(self):
        self.assertEqual(v.size_confidence(100, 500)[0], "high")
        self.assertEqual(v.size_confidence(400, 500)[0], "medium")
        self.assertEqual(v.size_confidence(900, 500)[0], "low")
        self.assertIn("over the 500 line limit", v.size_confidence(900, 500)[1])

    def test_without_a_limit_only_a_very_big_diff_drops_it(self):
        self.assertEqual(v.size_confidence(1999, None)[0], "high")
        self.assertEqual(v.size_confidence(v.BIG_DIFF + 1, None)[0], "medium")

    def test_an_incremental_pass_is_never_high(self):
        self.assertEqual(v.size_confidence(10, 5000, incremental=True)[0], "medium")

    def test_a_coverage_report_wins_over_the_size_guard(self):
        level, _ = v.confidence({"complete": True, "files_total": 3, "files_inspected": 3},
                                changed=99999, limit=10)
        self.assertEqual(level, "high")

    def test_a_missing_or_broken_report_falls_back(self):
        self.assertIsNone(v.read_coverage(""))
        self.assertIsNone(v.read_coverage("/nowhere/coverage.json"))
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "coverage.json")
            path.write_text("not json")
            self.assertIsNone(v.read_coverage(str(path)))
            path.write_text('{"complete": true, "files_total": 2, "files_inspected": 2}')
            self.assertEqual(v.read_coverage(str(path))["files_total"], 2)


class ConfidenceCapTest(unittest.TestCase):
    """Low confidence never lets a thin review call a pull request mergeable."""

    def combination(self, findings, level):
        coverage = {"high": {"complete": True, "files_total": 4, "files_inspected": 4},
                    "medium": {"complete": False, "files_total": 4, "files_inspected": 4},
                    "low": {"complete": False, "files_total": 100, "files_inspected": 1}}[level]
        health = v.assess(findings, coverage=coverage)
        self.assertEqual(health.confidence, level)
        return health

    def test_every_verdict_at_every_confidence(self):
        cases = [
            (None, "high", v.READY, "Ready to merge", False),
            (None, "medium", v.READY, "Ready to merge", False),
            (None, "low", v.CHANGES, "Needs a full review", True),
            (3, "high", v.NITS, "Mergeable, with nits", False),
            (2, "medium", v.NITS, "Mergeable, with nits", False),
            (2, "low", v.CHANGES, "Needs a full review", True),
            (1, "high", v.CHANGES, "Changes requested", False),
            (1, "medium", v.CHANGES, "Changes requested", False),
            (1, "low", v.CHANGES, "Changes requested", False),
            (0, "high", v.BLOCKED, "Do not merge", False),
            (0, "medium", v.BLOCKED, "Do not merge", False),
            (0, "low", v.BLOCKED, "Do not merge", False),
        ]
        for priority, level, key, headline, capped in cases:
            health = self.combination([] if priority is None else [finding(priority)], level)
            self.assertEqual((health.verdict, health.headline, health.capped), (key, headline, capped),
                             f"P{priority} at {level} confidence")

    def test_a_capped_verdict_says_why_and_stays_calm(self):
        health = self.combination([], "low")
        self.assertEqual(health.alert, v.INCOMPLETE_ALERT)
        self.assertIn("did not cover the whole pull request", health.note)
        # Nothing was found, so the score is untouched: it grades the code, the
        # confidence grades the review.
        self.assertEqual(health.score, 100)

    def test_a_real_finding_keeps_its_own_headline_and_alert(self):
        health = self.combination([finding(0)], "low")
        self.assertEqual((health.alert, health.note), ("CAUTION", ""))


class ScoreTest(unittest.TestCase):
    def score(self, priorities, signals=None):
        return v.score(v.deductions(priorities, signals or {}))

    def test_the_weights(self):
        self.assertEqual(self.score([]), 100)
        self.assertEqual(self.score([0]), 60)
        self.assertEqual(self.score([1]), 80)
        self.assertEqual(self.score([2]), 95)
        self.assertEqual(self.score([3]), 99)
        self.assertEqual(self.score([0, 1, 2, 3]), 34)

    def test_caps_stop_nits_outranking_one_real_bug(self):
        nits = self.score([2] * 20 + [3] * 20)
        self.assertEqual(nits, 80)  # -15 for the P2s, -5 for the P3s
        self.assertGreaterEqual(nits, self.score([1]))
        self.assertGreater(nits, self.score([0]))

    def test_every_priority_is_capped(self):
        self.assertEqual(self.score([0] * 10), 20)
        self.assertEqual(self.score([1] * 10), 40)

    def test_the_score_never_leaves_the_scale(self):
        self.assertEqual(self.score([0] * 3 + [1] * 5, {"checks": True, "conflicts": True, "draft": True}), 0)

    def test_pull_request_signals(self):
        self.assertEqual(self.score([], {"checks": True}), 90)
        self.assertEqual(self.score([], {"conflicts": True}), 90)
        self.assertEqual(self.score([], {"draft": True}), 95)
        self.assertEqual(self.score([], {"checks": True, "conflicts": True, "draft": True}), 75)

    def test_only_a_failed_rollup_counts(self):
        for state in ("SUCCESS", "PENDING", "EXPECTED", ""):
            self.assertFalse(v.pr_signals({"checks": state})["checks"], state)
        for state in ("FAILURE", "ERROR", "failure"):
            self.assertTrue(v.pr_signals({"checks": state})["checks"], state)

    def test_only_a_conflicting_merge_state_counts(self):
        self.assertTrue(v.pr_signals({"mergeable": "CONFLICTING"})["conflicts"])
        for state in ("MERGEABLE", "UNKNOWN", ""):
            self.assertFalse(v.pr_signals({"mergeable": state})["conflicts"], state)

    def test_no_signals_at_all_is_not_a_deduction(self):
        self.assertEqual(v.pr_signals(None), {})
        self.assertEqual(v.deductions([], None), [])


class BreakdownTest(unittest.TestCase):
    def test_every_deduction_has_a_row(self):
        health = v.assess([finding(0), finding(2), finding(2)], [carried(3)],
                          {"draft": True, "mergeable": "CONFLICTING", "checks": "FAILURE"})
        text = v.breakdown(health)
        self.assertIn("| Starting score | 100 |", text)
        self.assertIn("| 1 P0 finding | -40 |", text)
        self.assertIn("| 2 P2 findings | -10 |", text)
        self.assertIn("| 1 P3 finding | -1 |", text)
        self.assertIn("| Failing checks | -10 |", text)
        self.assertIn("| Merge conflicts | -10 |", text)
        self.assertIn("| Draft pull request | -5 |", text)
        self.assertIn("| **Health** | **%d / 100** |" % health.score, text)
        self.assertTrue(text.startswith("<details>"))
        self.assertTrue(text.rstrip().endswith("</details>"))

    def test_a_capped_row_says_so(self):
        text = v.breakdown(v.assess([finding(3) for _ in range(9)]))
        self.assertIn("| 9 P3 findings (capped) | -5 |", text)

    def test_a_clean_review_has_only_the_two_rows(self):
        rows = [row for row in v.breakdown(v.assess([])).splitlines() if row.startswith("|")]
        self.assertEqual(rows, ["| Signal | Effect |", "|---|---|", "| Starting score | 100 |",
                                "| **Health** | **100 / 100** |"])

    def test_the_line_names_verdict_score_and_confidence(self):
        health = v.assess([finding(1)], coverage={"complete": False, "files_total": 224,
                                                  "files_inspected": 38})
        self.assertEqual(v.line(health),
                         "**Changes requested** · Health 80/100 · "
                         "Confidence: low (partial review, 38/224 files inspected)")
        self.assertNotIn("*", v.plain_line(health))


class TrendTest(unittest.TestCase):
    def test_the_trend_comes_from_the_previous_state_marker(self):
        health = v.assess([finding(1)], previous={"hs": 55, "vd": "blocked"})
        self.assertEqual(health.previous_score, 55)
        self.assertEqual(health.previous_verdict, "blocked")
        self.assertEqual(health.delta, 25)
        self.assertEqual(health.trend, "health 55 -> 80 (+25 since last review)")

    def test_a_worse_score_reads_as_a_drop(self):
        health = v.assess([finding(0)], previous={"hs": 100})
        self.assertEqual(health.trend, "health 100 -> 60 (-40 since last review)")

    def test_no_change_says_so(self):
        self.assertEqual(v.assess([], previous={"hs": 100}).trend,
                         "health unchanged since the last review")

    def test_without_a_previous_score_there_is_no_trend(self):
        for previous in (None, {}, {"sha": "abc"}, {"hs": "not a number"}):
            health = v.assess([], previous=previous)
            self.assertEqual((health.trend, health.delta), ("", None), previous)

    def test_the_answer_survives_a_json_round_trip(self):
        health = v.assess([finding(0), finding(2)], [carried(3)], {"checks": "FAILURE"},
                          previous={"hs": 20, "vd": "blocked"})
        again = v.Health.from_dict(json.loads(json.dumps(health.as_dict())))
        self.assertEqual((again.verdict, again.score, again.confidence, again.capped),
                         (health.verdict, health.score, health.confidence, health.capped))
        self.assertEqual(again.deductions, health.deductions)
        self.assertEqual(again.trend, health.trend)

    def test_a_broken_round_trip_does_not_explode(self):
        blank = v.Health.from_dict({})
        self.assertEqual((blank.verdict, blank.score, blank.confidence), (v.READY, 0, "high"))
        self.assertEqual(v.Health.from_dict({"verdict": "nonsense"}).verdict, v.READY)


class GatingTest(unittest.TestCase):
    def test_fail_on_verdict_is_parsed_or_refused(self):
        self.assertIsNone(v.parse_threshold(""))
        self.assertIsNone(v.parse_threshold("none"))
        self.assertEqual(v.parse_threshold(" Blocked "), v.BLOCKED)
        self.assertEqual(v.parse_threshold("changes-requested"), v.CHANGES)
        with self.assertRaises(SystemExit):
            v.parse_threshold("P1")

    def test_a_gate_trips_at_its_own_level_and_worse(self):
        for key in v.ORDER:
            health = v.Health(key, 50, "high")
            self.assertFalse(v.fails(health, None))
            self.assertTrue(v.fails(health, key))
        self.assertTrue(v.fails(v.Health(v.BLOCKED, 0, "high"), v.CHANGES))
        self.assertFalse(v.fails(v.Health(v.NITS, 90, "high"), v.CHANGES))
        self.assertTrue(v.fails(v.Health(v.READY, 100, "high"), v.READY))

    def test_check_conclusions(self):
        cases = {
            (v.READY, "high"): "success",
            (v.NITS, "high"): "success",
            (v.READY, "medium"): "neutral",
            (v.NITS, "low"): "neutral",
            (v.CHANGES, "high"): "neutral",
            (v.CHANGES, "low"): "neutral",
            (v.BLOCKED, "high"): "failure",
            (v.BLOCKED, "low"): "failure",
        }
        for (key, level), expected in cases.items():
            self.assertEqual(v.check_conclusion(v.Health(key, 50, level)), expected, f"{key}/{level}")

    def test_a_gate_turns_the_check_red(self):
        health = v.Health(v.CHANGES, 80, "high")
        self.assertEqual(v.check_conclusion(health), "neutral")
        self.assertEqual(v.check_conclusion(health, v.CHANGES), "failure")
        self.assertEqual(v.check_conclusion(v.Health(v.READY, 100, "high"), v.BLOCKED), "success")


class ReviewEventTest(unittest.TestCase):
    def test_the_default_and_the_explicit_values(self):
        self.assertEqual(v.parse_event(""), "COMMENT")
        self.assertEqual(v.parse_event("comment"), "COMMENT")
        self.assertEqual(v.parse_event("REQUEST_CHANGES"), "REQUEST_CHANGES")
        with self.assertRaises(SystemExit):
            v.parse_event("APPROVE")

    def test_auto_follows_the_verdict(self):
        wanted = {v.READY: "COMMENT", v.NITS: "COMMENT",
                  v.CHANGES: "REQUEST_CHANGES", v.BLOCKED: "REQUEST_CHANGES"}
        for key, event in wanted.items():
            self.assertEqual(v.parse_event("auto", v.Health(key, 50, "high")), event, key)

    def test_auto_never_requests_changes_over_a_thin_review(self):
        capped = v.Health(v.CHANGES, 100, "low", capped=True)
        self.assertEqual(v.parse_event("auto", capped), "COMMENT")
        self.assertEqual(v.parse_event("auto"), "COMMENT")


class LabelTest(unittest.TestCase):
    def api(self, on_pr=(), fail=None):
        calls = []

        def call(method, path, token, payload=None, url=None):
            calls.append((method, path, payload))
            refusal = fail(method, path) if fail else None
            if refusal:
                raise refusal
            if method == "POST" and path.endswith("/issues/1/labels"):
                names = set(on_pr) | set((payload or {}).get("labels") or [])
                return [{"name": name} for name in sorted(names)]
            return {}

        return call, calls

    def test_the_verdict_label_is_created_then_applied(self):
        call, calls = self.api()
        self.assertEqual(v.apply_labels("o/r", "1", "t", v.BLOCKED, call), "codex: blocked")
        self.assertEqual([(m, p) for m, p, _ in calls],
                         [("POST", "/repos/o/r/labels"), ("POST", "/repos/o/r/issues/1/labels")])
        self.assertEqual(calls[0][2], {"name": "codex: blocked", "color": "b60205",
                                       "description": "Codex review: do not merge"})
        self.assertEqual(calls[1][2], {"labels": ["codex: blocked"]})

    def test_the_other_codex_labels_come_off(self):
        call, calls = self.api(on_pr=["codex: blocked", "codex: nits", "needs-qa"])
        v.apply_labels("o/r", "1", "t", v.READY, call)
        deletes = [p for m, p, _ in calls if m == "DELETE"]
        self.assertEqual(sorted(deletes), ["/repos/o/r/issues/1/labels/codex%3A%20blocked",
                                           "/repos/o/r/issues/1/labels/codex%3A%20nits"])
        self.assertNotIn("needs-qa", " ".join(deletes))

    def test_the_label_already_there_is_not_removed_again(self):
        call, calls = self.api(on_pr=["codex: ready"])
        v.apply_labels("o/r", "1", "t", v.READY, call)
        self.assertEqual([m for m, _, _ in calls], ["POST", "POST"])

    def test_a_label_that_already_exists_is_not_an_error(self):
        def exists(method, path):
            if path == "/repos/o/r/labels":
                return urllib.error.HTTPError("u", 422, "Validation Failed", {}, io.BytesIO(b"already_exists"))
            return None

        call, calls = self.api(fail=exists)
        self.assertEqual(v.apply_labels("o/r", "1", "t", v.NITS, call), "codex: nits")
        self.assertEqual(len(calls), 2)

    def test_403_warns_once_and_never_fails(self):
        def refused(method, path):
            return urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"no permission"))

        call, calls = self.api(fail=refused)
        with mock.patch("builtins.print") as printed:
            self.assertEqual(v.apply_labels("o/r", "1", "t", v.READY, call), "")
        self.assertEqual(len(calls), 1)  # it gives up at the first refusal
        warning = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("::warning::", warning)
        self.assertIn("issues: write", warning)

    def test_a_network_failure_is_survivable(self):
        def broken(method, path):
            return urllib.error.URLError("no route")

        call, _ = self.api(fail=broken)
        self.assertEqual(v.apply_labels("o/r", "1", "t", v.READY, call), "")

    def test_every_verdict_has_a_distinct_label_and_a_valid_colour(self):
        names = [v.label_name(key) for key in v.ORDER]
        self.assertEqual(len(set(names)), 4)
        for key in v.ORDER:
            name, colour, description = v.LABELS[key]
            self.assertTrue(name.startswith("codex: "), name)
            self.assertRegex(colour, r"\A[0-9a-f]{6}\Z")
            self.assertTrue(description)


if __name__ == "__main__":
    unittest.main()


class EmptyScopeTest(unittest.TestCase):
    def test_no_files_in_scope_is_not_a_partial_review(self):
        # The coverage writer reports complete=false for an empty diff; nothing was missed.
        level, reason = verdict.coverage_confidence({"mode": "single", "complete": False, "files_total": 0,
                                                     "files_inspected": 0, "uncovered": [], "shards": 0, "passes": 1})
        self.assertEqual((level, reason), ("high", "no changed files in scope"))

import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import full_review  # noqa: E402
import gitrepo  # noqa: E402
import publish_review  # noqa: E402
import shards  # noqa: E402

# A stand-in for the Codex CLI: it runs the diff it was given, records that the
# way a real rollout does, and reports one finding in the first file it sees.
FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
base = args[args.index("--base") + 1]
out = pathlib.Path(args[args.index("-o") + 1])
home = pathlib.Path(os.environ["CODEX_HOME"])
diff = subprocess.run(["git", "diff", base], capture_output=True, text=True).stdout
sessions = home / "sessions" / "2026" / "01" / "01"
sessions.mkdir(parents=True, exist_ok=True)
events = [
    {"type": "session_meta", "payload": {"cwd": os.getcwd()}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["/bin/bash", "-lc", "git diff " + base],
        "cwd": "file://" + os.getcwd(), "stdout": diff}}},
    {"type": "token_usage_record", "payload": {"thread_token_usage": {
        "input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20,
        "reasoning_output_tokens": 5}, "usage": {
        "input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20,
        "reasoning_output_tokens": 5}}},
]
name = "rollout-2026-01-01T00-00-00-%s.jsonl" % os.path.basename(str(home))
(sessions / name).write_text("".join(json.dumps(e) + chr(10) for e in events), encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))

files = sorted({line.split(" b/")[-1].strip() for line in diff.splitlines()
                if line.startswith("diff --git ")})
where = files[0] if files else "unknown"
body = ["Reviewed " + str(len(files)) + " file(s).", "", "Full review comments:", ""]
body.append("- [P2] Something in " + where + " \\u2014 " + os.getcwd() + "/" + where + ":1")
body.append("  The explanation for " + where + ".")
out.write_text(chr(10).join(body) + chr(10), encoding="utf-8")
'''
SLOW_CODEX = "#!/bin/sh\nsleep 30\n"
BROKEN_CODEX = "#!/bin/sh\necho 'boom' >&2\nexit 3\n"


def write_binary(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def finding(path, title, priority=2, start=1, end=1, body="Because.", suggestion=""):
    return {"priority": priority, "title": title, "path": path, "start": start, "end": end,
            "body": body, "suggestion": suggestion,
            "fingerprint": __import__("history").fingerprint(path, title)}


class InstructionsTest(unittest.TestCase):
    """Each pass gets its own instructions without breaking the TOML."""

    def test_extra_instructions_are_appended_to_the_existing_ones(self):
        config = ('model = "gpt"\ndeveloper_instructions = "Keep the layout."\n\n'
                  '[shell_environment_policy]\ninherit = "core"\n')
        out = full_review.with_instructions(config, "Only these files.")
        line = [row for row in out.splitlines() if row.startswith("developer_instructions")][0]
        self.assertEqual(json.loads(line.split(" = ", 1)[1]),
                         "Keep the layout.\n\nOnly these files.")
        self.assertIn('[shell_environment_policy]', out)

    def test_instructions_are_added_before_the_first_table(self):
        config = 'model = "gpt"\n\n# a comment\n[shell_environment_policy]\ninherit = "core"\n'
        out = full_review.with_instructions(config, "Only these files.")
        rows = out.splitlines()
        self.assertLess(rows.index([r for r in rows if r.startswith("developer_instructions")][0]),
                        rows.index("[shell_environment_policy]"))

    def test_no_extra_instructions_leaves_the_config_alone(self):
        config = 'model = "gpt"\n'
        self.assertEqual(full_review.with_instructions(config, ""), config)

    def test_a_pass_home_is_a_private_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = pathlib.Path(tmp) / "home"
            (template / "sessions").mkdir(parents=True)
            (template / "sessions" / "old.jsonl").write_text("stale", encoding="utf-8")
            (template / "config.toml").write_text('model = "gpt"\n', encoding="utf-8")
            (template / "auth.json").write_text("{}", encoding="utf-8")
            home = pathlib.Path(full_review.pass_home(str(template), pathlib.Path(tmp) / "p1",
                                                     "Shard note."))
            self.assertTrue((home / "auth.json").is_file())
            self.assertFalse((home / "sessions").exists())  # no other pass's rollouts
            self.assertIn("Shard note.", (home / "config.toml").read_text(encoding="utf-8"))


class MergeTest(unittest.TestCase):
    """Every pass's findings, folded into one review."""

    def job(self, identifier, findings, kind="shard", summary="Looked at it."):
        one = full_review.Pass(identifier, kind, identifier, [])
        one.findings = findings
        one.summary = summary
        one.status = "ok"
        return one

    def test_an_empty_cross_pass_does_not_leak_its_heading(self):
        # Codex writes "Full review comments: None." when a pass has nothing; the
        # merged review must still have exactly one findings heading, with the
        # per-area summaries in the summary part.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            review = pathlib.Path(tmp, "cross.md")
            review.write_text("No cross-area issues found.\n\nFull review comments:\nNone.\n", encoding="utf-8")
            cross = full_review.Pass("cross", "cross", "cross", [])
            cross.review_file, cross.cwd, cross.status = str(review), tmp, "ok"
            full_review.collect(cross)
        self.assertEqual(cross.summary, "No cross-area issues found.")
        shard = self.job("shard-1", [finding("a.ts", "Off by one in the loop")], summary="The loop is off by one.")
        text = full_review.render(full_review.merge([shard, cross]), [shard, cross],
                                  {"files_total": 1, "shards": 1})
        self.assertEqual(text.count("Full review comments:"), 1)
        summary, findings = publish_review.parse_review(text, "")
        self.assertIn("The loop is off by one.", summary)
        self.assertIn("Across areas:** no issues that span areas", summary)
        self.assertEqual(len(findings), 1)

    def test_the_same_finding_from_two_passes_is_reported_once(self):
        left = self.job("shard-1", [finding("a.ts", "Off by one in the loop")])
        right = self.job("cross", [finding("a.ts", "Off by one in the loop")], kind="cross")
        merged = full_review.merge([left, right])
        self.assertEqual(len(merged), 1)

    def test_a_reworded_finding_on_the_same_lines_is_the_same_finding(self):
        left = self.job("shard-1", [finding("a.ts", "Discount percentage is not divided by 100")])
        right = self.job("shard-2", [finding("a.ts", "Percentage discount not divided by 100",
                                             start=3)])
        self.assertEqual(len(full_review.merge([left, right])), 1)

    def test_similar_wording_far_apart_stays_two_findings(self):
        left = self.job("shard-1", [finding("a.ts", "Unchecked index access", start=10)])
        right = self.job("shard-2", [finding("a.ts", "Unchecked index access on the second page",
                                             start=400)])
        self.assertEqual(len(full_review.merge([left, right])), 2)
        # The same wording nearby is one issue two passes both noticed.
        near = self.job("shard-2", [finding("a.ts", "Unchecked index access on the second page",
                                            start=14)])
        self.assertEqual(len(full_review.merge([left, near])), 1)

    def test_the_worst_priority_and_the_fix_survive_the_merge(self):
        left = self.job("shard-1", [finding("a.ts", "Same issue", priority=3)])
        right = self.job("cross", [finding("a.ts", "Same issue", priority=0,
                                           suggestion="fixed()", body="A much longer body.")],
                         kind="cross")
        merged = full_review.merge([left, right])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["priority"], 0)
        self.assertEqual(merged[0]["suggestion"], "fixed()")
        self.assertEqual(merged[0]["body"], "A much longer body.")

    def test_findings_come_out_worst_first_then_by_place(self):
        one = self.job("shard-1", [finding("b.ts", "Timeout never applied", priority=3),
                                   finding("a.ts", "Race between writers", priority=1, start=9),
                                   finding("a.ts", "Missing null guard", priority=1, start=2)])
        merged = full_review.merge([one])
        self.assertEqual([(f["priority"], f["path"], f["start"]) for f in merged],
                         [(1, "a.ts", 2), (1, "a.ts", 9), (3, "b.ts", 1)])

    def test_the_merged_review_parses_back_as_a_codex_review(self):
        one = self.job("shard-1", [finding("src/a.ts", "Divide by 100", priority=1, start=4, end=6,
                                           body="Line one.\n\n    indented", suggestion="x = 1")])
        two = self.job("cross", [finding("src/b.ts", "Caller not updated", priority=2)],
                       kind="cross", summary="The wiring is fine otherwise.")
        report = {"files_total": 4, "shards": 2, "passes": 2}
        text = full_review.render(full_review.merge([one, two]), [one, two], report)
        summary, parsed = publish_review.parse_review(text, "")
        self.assertIn("Full review: 4 changed file(s) in 2 shard(s), 2 pass(es).", summary)
        self.assertIn("The wiring is fine otherwise.", summary)
        self.assertEqual([(f["priority"], f["path"], f["start"], f["end"]) for f in parsed],
                         [(1, "src/a.ts", 4, 6), (2, "src/b.ts", 1, 1)])
        self.assertEqual(parsed[0]["suggestion"], "x = 1")
        self.assertIn("    indented", parsed[0]["body"])

    def test_a_pass_that_did_not_run_is_named_in_the_review(self):
        good = self.job("shard-1", [finding("a.ts", "Something")])
        bad = full_review.Pass("shard-2", "shard", "src/b (3 files)", [])
        bad.status, bad.error = "timeout", "no answer within 10 minute(s)"
        text = full_review.render(full_review.merge([good]), [good, bad],
                                  {"files_total": 4, "shards": 2, "passes": 1})
        self.assertIn("Not reviewed: src/b (3 files) (no answer within 10 minute(s)).", text)


class TimeoutTest(unittest.TestCase):
    """A pass that hangs or fails is reported, never silently dropped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pass_for(self, script):
        job = full_review.Pass("shard-1", "shard", "src", [])
        job.base, job.cwd = "HEAD", str(self.root)
        job.review_file = str(self.root / "review.md")
        job.events_file = str(self.root / "events.jsonl")
        job.home = str(self.root / "home")
        pathlib.Path(job.home).mkdir(exist_ok=True)
        settings = full_review.Settings({"RUNNER_TEMP": str(self.root)})
        settings.codex = write_binary(self.root / "fake-codex", script)
        return job, settings

    def test_a_pass_that_runs_out_of_time_says_so(self):
        job, settings = self.pass_for(SLOW_CODEX)
        full_review.run_codex(job, settings, seconds=1)
        self.assertEqual(job.status, "timeout")
        self.assertIn("minute", job.error)
        self.assertLess(job.seconds, 25)

    def test_a_pass_that_fails_keeps_the_reason(self):
        job, settings = self.pass_for(BROKEN_CODEX)
        full_review.run_codex(job, settings, seconds=20)
        self.assertEqual(job.status, "failed")
        self.assertIn("boom", job.error)

    def test_a_pass_with_no_time_left_is_skipped_not_started(self):
        job, settings = self.pass_for(SLOW_CODEX)
        trees = full_review.Worktrees(str(self.root), self.root / "wt", 1)
        full_review.review_all([job], settings, trees, deadline=0,
                               base="x", head="y", index_root=self.root)
        self.assertEqual(job.status, "skipped")
        self.assertIn("ran out of time", job.error)


class SettingsTest(unittest.TestCase):
    def test_numbers_fall_back_to_the_default(self):
        self.assertEqual(full_review.number("", 7), 7)
        self.assertEqual(full_review.number("nine", 7), 7)
        self.assertEqual(full_review.number("12", 7), 12)
        self.assertEqual(full_review.number("0", 7, lowest=1), 1)

    def test_the_defaults_are_the_documented_ones(self):
        settings = full_review.Settings({"RUNNER_TEMP": "/tmp/x"})
        self.assertEqual(settings.shard_lines, 2500)
        self.assertEqual(settings.parallel, 4)
        self.assertEqual(settings.max_shards, 24)
        self.assertEqual(settings.pass_timeout, 600)
        self.assertEqual(settings.budget, 1200)


class EndToEndTest(unittest.TestCase):
    """A whole full review against a real repository, with a stand-in reviewer."""

    def setUp(self):
        self.repo = gitrepo.Repo()
        self.repo.write("src/app.ts", "one\n")
        self.repo.write("web/page.ts", "two\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/app.ts", "one\n" + "line\n" * 40)
        self.repo.write("web/page.ts", "two\n" + "line\n" * 40)
        self.head = self.repo.commit("head")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "codex-home").mkdir()
        (self.root / "codex-home" / "config.toml").write_text(
            'model = "gpt"\n\n[shell_environment_policy]\ninherit = "core"\n', encoding="utf-8")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def run_full(self, **extra):
        env = {
            "RUNNER_TEMP": str(self.root),
            "CODEX_HOME": str(self.root / "codex-home"),
            "CODEX_BIN": write_binary(self.root / "fake-codex", FAKE_CODEX),
            "BASE_REF": self.base,
            "REVIEW_FILE": str(self.root / "codex-review.md"),
            "EVENTS_FILE": str(self.root / "codex-review-events.jsonl"),
            "COVERAGE_FILE": str(self.root / "codex-review-coverage.json"),
            "CODEX_HOMES_FILE": str(self.root / "codex-review-homes.txt"),
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "SHARD_LINES": "10",
            "MAX_PARALLEL": "2",
            "PASS_TIMEOUT_MINUTES": "2",
            "REVIEW_BUDGET_MINUTES": "5",
        }
        env.update(extra)
        here = os.getcwd()
        os.chdir(str(self.repo.path))
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                return full_review.main()
        finally:
            os.chdir(here)

    def test_a_full_review_shards_measures_and_merges(self):
        self.assertEqual(self.run_full(), 0)
        report = json.loads((self.root / "codex-review-coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(report["mode"], "full")
        self.assertEqual(report["files_total"], 2)
        self.assertEqual(report["files_inspected"], 2)
        self.assertEqual(report["uncovered"], [])
        self.assertTrue(report["complete"])
        self.assertEqual(report["shards"], 2)
        # Two shard passes plus the cross-cutting one.
        self.assertEqual(report["passes"], 3)

        review = (self.root / "codex-review.md").read_text(encoding="utf-8")
        summary, findings = publish_review.parse_review(review, "")
        self.assertIn("2 changed file(s) in 2 shard(s), 3 pass(es)", summary)
        self.assertEqual(sorted(f["path"] for f in findings), ["src/app.ts", "web/page.ts"])
        # The paths are repository relative, whichever worktree found them.
        self.assertFalse(any(f["path"].startswith("/") for f in findings))

    def test_every_pass_has_its_own_home_and_they_are_all_billed(self):
        self.run_full()
        homes = [line for line in
                 (self.root / "codex-review-homes.txt").read_text(encoding="utf-8").splitlines()
                 if line]
        self.assertEqual(len(homes), 3)
        self.assertEqual(len(set(homes)), 3)
        import usage
        totals, requests = usage.collect([pathlib.Path(home) / "sessions" for home in homes])
        self.assertEqual(totals["input_tokens"], 300)  # 100 per pass
        self.assertEqual(totals["output_tokens"], 60)
        self.assertEqual(len(requests), 3)

    def test_each_shard_pass_only_saw_its_own_shard(self):
        self.run_full()
        passes = sorted((self.root / "codex-review-passes").iterdir())
        shard_reviews = [path / "review.md" for path in passes if path.name.startswith("shard-")]
        self.assertEqual(len(shard_reviews), 2)
        for review in shard_reviews:
            # The stand-in reports how many files its diff showed: one each.
            self.assertIn("Reviewed 1 file(s).", review.read_text(encoding="utf-8"))

    def test_the_cross_cutting_pass_sees_the_whole_diff(self):
        self.run_full()
        cross = (self.root / "codex-review-passes" / "cross" / "review.md")
        self.assertIn("Reviewed 2 file(s).", cross.read_text(encoding="utf-8"))
        config = (self.root / "codex-review-homes" / "cross" / "config.toml")
        instructions = config.read_text(encoding="utf-8")
        self.assertIn("span more than one of those areas", instructions)

    def test_the_worktrees_and_the_repository_are_left_clean(self):
        before = (self.repo.refs(), self.repo.head())
        self.run_full()
        self.assertEqual((self.repo.refs(), self.repo.head()), before)
        self.assertEqual(self.repo.dirty(), "")
        listed = shards.git(["worktree", "list", "--porcelain"], str(self.repo.path))
        self.assertEqual(listed.count("worktree "), 1)

    def test_one_shard_is_reviewed_in_one_pass_against_the_base_ref(self):
        self.assertEqual(self.run_full(SHARD_LINES="100000"), 0)
        report = json.loads((self.root / "codex-review-coverage.json").read_text(encoding="utf-8"))
        self.assertEqual((report["shards"], report["passes"]), (1, 1))
        self.assertTrue(report["complete"])
        self.assertIn("Reviewed 2 file(s).",
                      (self.root / "codex-review-passes" / "whole" / "review.md")
                      .read_text(encoding="utf-8"))

    def test_a_reviewer_that_always_fails_fails_the_run(self):
        self.assertEqual(self.run_full(CODEX_BIN=write_binary(self.root / "broken", BROKEN_CODEX)), 1)
        report = json.loads((self.root / "codex-review-coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(report["files_inspected"], 0)
        self.assertEqual(report["passes"], 0)
        self.assertEqual(report["uncovered"], ["src/app.ts", "web/page.ts"])

    def test_the_path_filters_decide_what_has_to_be_covered(self):
        self.assertEqual(self.run_full(EXCLUDE_PATHS="web/**"), 0)
        report = json.loads((self.root / "codex-review-coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(report["files_total"], 1)
        self.assertTrue(report["complete"])


if __name__ == "__main__":
    unittest.main()

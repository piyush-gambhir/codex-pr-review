import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import coverage  # noqa: E402
import gitrepo  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOME = ROOT / "tests" / "fixtures" / "coverage-home"
WORK = "/home/runner/work/repo/repo"
# Everything the fixture's pull request changes.
CHANGED = ["src/app.ts", "src/util.ts", "src/late.ts", "src/models.py",
           "src/secret.py", "docs/guide.md", "src/deep/thing.ts", "src/never.ts"]


def run(command, output="", root=WORK, direct=None):
    return coverage.Record(command=command, output=output, root=root, direct=direct)


def seen(records, candidates=None, scope=None):
    return coverage.inspected(records, candidates or CHANGED, [WORK], scope=scope)


class RulesTest(unittest.TestCase):
    """What counts as having read a file, and what does not."""

    def test_reading_a_file_counts(self):
        for command in ("cat src/app.ts", "nl -ba src/app.ts", "sed -n '1,120p' src/app.ts",
                        "head -50 ./src/app.ts", "tail -n 20 " + WORK + "/src/app.ts"):
            self.assertEqual(seen([run(command)]), {"src/app.ts"}, command)

    def test_a_sed_script_is_not_mistaken_for_a_path(self):
        found = seen([run("sed -n '1,80p' src/app.ts")])
        self.assertEqual(found, {"src/app.ts"})

    def test_listing_and_searching_do_not_count(self):
        for command in ("ls src", "rg -n 'token' src/secret.py", "grep -rn x src/app.ts",
                        "find src -name '*.ts'", "wc -l src/app.ts"):
            self.assertEqual(seen([run(command)]), set(), command)

    def test_a_summary_diff_names_files_without_showing_them(self):
        for command in ("git diff abc123 --stat", "git diff --name-only abc123",
                        "git diff --numstat abc123", "git diff --name-status abc123"):
            self.assertEqual(seen([run(command, output=" src/app.ts | 2 +\n")]), set(), command)

    def test_a_diff_counts_the_files_its_output_really_showed(self):
        output = ("diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n"
                  "@@ -1 +1,2 @@\n+added\n")
        self.assertEqual(seen([run("git diff abc123", output=output)]), {"src/app.ts"})

    def test_a_truncated_diff_stops_where_the_output_stopped(self):
        output = ("diff --git a/src/app.ts b/src/app.ts\n+++ b/src/app.ts\n+one\n"
                  "diff --git a/src/util.ts b/src/util.ts\n+++ b/src/util.ts\n+two\n"
                  "[... output truncated ...]")
        self.assertEqual(seen([run("git diff abc123", output=output)]),
                         {"src/app.ts", "src/util.ts"})

    def test_a_whole_diff_with_no_recorded_output_falls_back_to_the_shard(self):
        found = seen([run("git diff abc123")], scope=["src/app.ts", "src/util.ts"])
        self.assertEqual(found, {"src/app.ts", "src/util.ts"})
        # Without a scope (the cross-cutting pass) the claim is not made.
        self.assertEqual(seen([run("git diff abc123")], scope=[]), set())

    def test_a_diff_limited_to_paths_counts_only_those(self):
        self.assertEqual(seen([run("git diff abc123 -- src/app.ts")], scope=CHANGED),
                         {"src/app.ts"})

    def test_git_log_only_counts_with_a_patch(self):
        patch = "diff --git a/src/app.ts b/src/app.ts\n+++ b/src/app.ts\n"
        self.assertEqual(seen([run("git log --oneline -5", output=patch)]), set())
        self.assertEqual(seen([run("git log -p -2", output=patch)]), {"src/app.ts"})

    def test_a_compound_command_is_split_into_its_parts(self):
        command = "pwd && cat src/app.ts | head -20 ; ls src ; nl -ba src/models.py"
        self.assertEqual(seen([run(command)]), {"src/app.ts", "src/models.py"})

    def test_a_quoted_separator_does_not_split_a_command(self):
        self.assertEqual(coverage.split_commands("rg 'a;b' src; cat src/app.ts"),
                         ["rg 'a;b' src", "cat src/app.ts"])

    def test_an_unrelated_file_is_never_counted(self):
        self.assertEqual(seen([run("cat package.json; cat src/other/app.ts")]), set())

    def test_a_renamed_diff_header_counts_both_paths(self):
        output = "diff --git a/src/app.ts b/src/util.ts\nsimilarity index 98%\n"
        self.assertEqual(seen([run("git diff abc123", output=output)]),
                         {"src/app.ts", "src/util.ts"})

    def test_a_structured_read_event_needs_no_command(self):
        self.assertEqual(seen([run("", direct=["src/models.py"])]), {"src/models.py"})


class RolloutTest(unittest.TestCase):
    """The same rules against a captured `codex exec review` rollout."""

    def setUp(self):
        self.records = coverage.records([str(HOME / "sessions")])

    def test_every_command_shape_in_the_rollout_is_read(self):
        commands = [record.command for record in self.records if record.command]
        self.assertTrue(any("git diff --stat" in c for c in commands), commands)
        self.assertTrue(any(c.startswith("git diff 1111111") for c in commands), commands)
        self.assertTrue(any("nl -ba src/models.py" in c for c in commands), commands)
        # The `exec` tool's output is linked back to its call by call_id.
        diff = next(r for r in self.records if r.command.startswith("git diff 1111111"))
        self.assertIn("diff --git a/src/app.ts", diff.output)

    def test_what_the_reviewer_actually_read(self):
        found = coverage.inspected(self.records, CHANGED, [WORK])
        self.assertEqual(found, {"src/app.ts", "src/util.ts", "src/models.py",
                                 "docs/guide.md", "src/deep/thing.ts"})

    def test_the_files_it_only_listed_searched_or_never_opened_are_uncovered(self):
        report = coverage.report("single", CHANGED,
                                 coverage.inspected(self.records, CHANGED, [WORK]))
        self.assertEqual(report["uncovered"], ["src/late.ts", "src/never.ts", "src/secret.py"])
        self.assertFalse(report["complete"])
        self.assertEqual((report["files_total"], report["files_inspected"]), (8, 5))

    def test_the_rollout_fixture_carries_no_personal_paths(self):
        text = (HOME / "sessions" / "2026" / "01" / "01").glob("rollout-*.jsonl")
        for path in text:
            body = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", body)
            self.assertNotIn("piyush", body.lower())


class ReportTest(unittest.TestCase):
    """The contract other steps read, and the line under the review."""

    def test_the_shape_is_the_contract(self):
        report = coverage.report("full", ["a.ts", "b.ts"], ["a.ts"], shard_count=2, passes=4)
        self.assertEqual(sorted(report), ["complete", "files_inspected", "files_total",
                                          "mode", "passes", "shards", "uncovered"])
        self.assertEqual(report, {"mode": "full", "complete": False, "files_total": 2,
                                  "files_inspected": 1, "uncovered": ["b.ts"],
                                  "shards": 2, "passes": 4})

    def test_complete_means_every_changed_file(self):
        self.assertTrue(coverage.report("full", ["a.ts"], ["a.ts"])["complete"])
        self.assertFalse(coverage.report("full", [], [])["complete"])

    def test_something_read_that_was_not_in_the_diff_is_not_counted(self):
        report = coverage.report("single", ["a.ts"], ["a.ts", "elsewhere.ts"])
        self.assertEqual(report["files_inspected"], 1)

    def test_the_meta_line(self):
        self.assertEqual(
            coverage.summary({"mode": "full", "files_total": 224, "files_inspected": 224,
                              "passes": 12}),
            "Coverage 224/224 files (full, 12 passes)")
        self.assertEqual(
            coverage.summary({"mode": "single", "files_total": 6, "files_inspected": 4,
                              "passes": 1}),
            "Coverage 4/6 files (single, 1 pass)")
        self.assertEqual(coverage.summary({}), "")
        self.assertEqual(coverage.summary({"files_total": 0}), "")


class SingleModeFileTest(unittest.TestCase):
    """A single review measures itself, so both modes publish the same contract."""

    def setUp(self):
        self.repo = gitrepo.Repo()
        self.repo.write("src/app.ts", "one\n")
        self.repo.write("src/never.ts", "two\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/app.ts", "one\ntwo\n")
        self.repo.write("src/never.ts", "two\nthree\n")
        self.repo.commit("head")
        self.tmp = tempfile.TemporaryDirectory()
        self.coverage_file = str(pathlib.Path(self.tmp.name) / "coverage.json")
        self.outputs = str(pathlib.Path(self.tmp.name) / "outputs")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def measure(self, commands):
        """Run coverage.py's main against a home holding one made-up rollout."""
        home = pathlib.Path(self.tmp.name) / "home" / "sessions" / "2026" / "01" / "01"
        home.mkdir(parents=True, exist_ok=True)
        events = [{"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "command": ["/bin/bash", "-lc", command],
            "cwd": "file://" + str(self.repo.path), "stdout": output}}}
            for command, output in commands]
        (home / "rollout-2026-01-01T00-00-00-x.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        env = {
            "COVERAGE_FILE": self.coverage_file,
            "REVIEW_MODE": "single",
            "BASE_REF": self.base,
            "CODEX_HOME": str(pathlib.Path(self.tmp.name) / "home"),
            "REVIEW_WORKSPACE": str(self.repo.path),
            "GITHUB_OUTPUT": self.outputs,
        }
        here = os.getcwd()
        os.chdir(str(self.repo.path))
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                coverage.main()
        finally:
            os.chdir(here)
        return json.loads(pathlib.Path(self.coverage_file).read_text(encoding="utf-8"))

    def test_a_single_review_writes_the_coverage_file(self):
        report = self.measure([("cat src/app.ts", "one\ntwo\n")])
        self.assertEqual(report["mode"], "single")
        self.assertEqual(report["files_total"], 2)
        self.assertEqual(report["files_inspected"], 1)
        self.assertEqual(report["uncovered"], ["src/never.ts"])
        self.assertFalse(report["complete"])
        self.assertEqual((report["shards"], report["passes"]), (1, 1))
        written = pathlib.Path(self.outputs).read_text(encoding="utf-8")
        self.assertIn("files-total=2", written)
        self.assertIn("files-inspected=1", written)
        self.assertIn("complete=false", written)

    def test_a_single_review_that_read_the_whole_diff_is_complete(self):
        diff = ("diff --git a/src/app.ts b/src/app.ts\n+++ b/src/app.ts\n+two\n"
                "diff --git a/src/never.ts b/src/never.ts\n+++ b/src/never.ts\n+three\n")
        report = self.measure([("git diff " + self.base, diff)])
        self.assertTrue(report["complete"])
        self.assertEqual(report["uncovered"], [])

    def test_an_existing_file_is_read_back_rather_than_recomputed(self):
        pathlib.Path(self.coverage_file).write_text(json.dumps({
            "mode": "full", "complete": True, "files_total": 9, "files_inspected": 9,
            "uncovered": [], "shards": 3, "passes": 5}), encoding="utf-8")
        report = self.measure([("cat src/app.ts", "")])
        self.assertEqual((report["mode"], report["files_total"], report["passes"]),
                         ("full", 9, 5))
        self.assertIn("files-total=9", pathlib.Path(self.outputs).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

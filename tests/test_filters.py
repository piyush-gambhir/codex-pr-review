import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import filters  # noqa: E402


class GlobTest(unittest.TestCase):
    def assertMatches(self, pattern, paths, misses=()):
        for path in paths:
            self.assertTrue(filters.matches(path, pattern), f"{pattern} should match {path}")
        for path in misses:
            self.assertFalse(filters.matches(path, pattern), f"{pattern} should not match {path}")

    def test_star_stays_inside_one_segment(self):
        self.assertMatches("src/*.ts", ["src/a.ts"], ["src/nested/a.ts", "a.ts", "src/a.tsx"])

    def test_double_star_spans_segments_including_none(self):
        self.assertMatches("src/**/*.ts", ["src/a.ts", "src/app/a.ts", "src/app/deep/a.ts"], ["lib/a.ts"])

    def test_double_star_both_ends(self):
        self.assertMatches("**/gen/**", ["gen/a.ts", "src/gen/a.ts", "a/b/gen/c/d.ts"], ["src/general.ts", "gen"])

    def test_trailing_double_star_covers_a_whole_tree(self):
        self.assertMatches("vendor/**", ["vendor/a.ts", "vendor/a/b.ts"], ["src/vendor/a.ts"])

    def test_bare_pattern_also_matches_the_file_name(self):
        self.assertMatches("*.lock", ["yarn.lock", "sub/dir/yarn.lock"], ["yarn.lock.md"])
        # With a separator it is anchored at the repository root instead.
        self.assertMatches("dist/*.js", ["dist/a.js"], ["pkg/dist/a.js"])

    def test_question_mark_and_character_class(self):
        self.assertMatches("src/a?.ts", ["src/ab.ts"], ["src/a.ts", "src/a/b.ts"])
        self.assertMatches("src/[ab].ts", ["src/a.ts", "src/b.ts"], ["src/c.ts"])
        self.assertMatches("src/[!ab].ts", ["src/c.ts"], ["src/a.ts"])

    def test_leading_dot_slash_and_slash_are_ignored(self):
        self.assertMatches("./src/**", ["src/a.ts", "./src/a.ts"])
        self.assertMatches("/src/a.ts", ["src/a.ts"])

    def test_dotted_directories_are_not_eaten(self):
        self.assertMatches(".github/**", [".github/workflows/ci.yml"], ["github/ci.yml"])

    def test_empty_pattern_matches_nothing(self):
        self.assertFalse(filters.matches("a.ts", ""))
        self.assertFalse(filters.matches("", "**"))


class ParsePatternsTest(unittest.TestCase):
    def test_newlines_commas_quotes_and_comments(self):
        text = " src/**/*.ts , 'lib/**' \n\n# a comment\n\"**/gen/**\"\n"
        self.assertEqual(filters.parse_patterns(text), ["src/**/*.ts", "lib/**", "**/gen/**"])

    def test_empty(self):
        self.assertEqual(filters.parse_patterns(""), [])
        self.assertEqual(filters.parse_patterns(None), [])


class PathFilterTest(unittest.TestCase):
    def test_no_patterns_allows_everything(self):
        pf = filters.PathFilter()
        self.assertFalse(pf.active())
        self.assertTrue(pf.allows("anything/at/all.ts"))

    def test_include_only(self):
        pf = filters.PathFilter(include=["src/**"])
        self.assertTrue(pf.active())
        self.assertTrue(pf.allows("src/a.ts"))
        self.assertFalse(pf.allows("tests/a.ts"))

    def test_exclude_wins_over_include(self):
        pf = filters.PathFilter(include=["src/**"], exclude=["**/gen/**"])
        self.assertTrue(pf.allows("src/a.ts"))
        self.assertFalse(pf.allows("src/gen/a.ts"))

    def test_from_env(self):
        pf = filters.PathFilter.from_env({"INCLUDE_PATHS": "src/**", "EXCLUDE_PATHS": "*.lock, **/gen/**"})
        self.assertEqual((pf.include, pf.exclude), (["src/**"], ["*.lock", "**/gen/**"]))


class LimitTest(unittest.TestCase):
    def test_parse_limit(self):
        self.assertIsNone(filters.parse_limit(""))
        self.assertIsNone(filters.parse_limit("none"))
        self.assertIsNone(filters.parse_limit("0"))
        self.assertEqual(filters.parse_limit(" 500 "), 500)
        with self.assertRaises(SystemExit):
            filters.parse_limit("many")

    def test_parse_mode(self):
        self.assertEqual(filters.parse_mode(""), "warn")
        self.assertEqual(filters.parse_mode("SKIP"), "skip")
        with self.assertRaises(SystemExit):
            filters.parse_mode("abort")

    def test_decisions(self):
        self.assertEqual(filters.guard_decision(9000, None, "skip"), (False, ""))
        self.assertEqual(filters.guard_decision(500, 500, "skip"), (False, ""))
        skip, note = filters.guard_decision(1200, 500, "skip")
        self.assertEqual((skip, note), (True, "PR too large to review (1200 changed lines, limit 500)."))
        warn, note = filters.guard_decision(1200, 500, "warn")
        self.assertFalse(warn)
        self.assertIn("1200 changed lines", note)
        self.assertIn("may be incomplete", note)


def git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=repo, check=True, capture_output=True, text=True)


class ChangedLinesTest(unittest.TestCase):
    """A throwaway repository, so `git diff --numstat` is exercised for real."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = cls.repo = cls.tmp.name
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "Test")
        pathlib.Path(repo, "src").mkdir()
        pathlib.Path(repo, "gen").mkdir()
        pathlib.Path(repo, "src/a.ts").write_text("one\ntwo\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        git(repo, "checkout", "-qb", "feature")
        pathlib.Path(repo, "src/a.ts").write_text("one\ntwo\nthree\nfour\n")  # 2 added
        pathlib.Path(repo, "gen/big.ts").write_text("x\n" * 50)  # 50 added, excluded
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "feature")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counts_added_and_deleted(self):
        self.assertEqual(filters.changed_lines("main", self.repo), 52)

    def test_excluded_paths_do_not_count(self):
        pf = filters.PathFilter(exclude=["**/gen/**"])
        self.assertEqual(filters.changed_lines("main", self.repo, pf), 2)

    def test_include_paths_narrow_the_count(self):
        pf = filters.PathFilter(include=["gen/**"])
        self.assertEqual(filters.changed_lines("main", self.repo, pf), 50)

    def test_counts_from_the_merge_base_not_the_branch_tip(self):
        # A commit only on main must not show up as a deletion on the feature side.
        git(self.repo, "checkout", "-q", "main")
        pathlib.Path(self.repo, "src/only-on-main.ts").write_text("m\n" * 30)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "main moves on")
        git(self.repo, "checkout", "-q", "feature")
        self.assertEqual(filters.changed_lines("main", self.repo), 52)

    def test_missing_base_ref_counts_nothing(self):
        self.assertEqual(filters.changed_lines("", self.repo), 0)

    def test_main_writes_outputs(self):
        with tempfile.TemporaryDirectory() as out_dir:
            out = pathlib.Path(out_dir, "output")
            env = {"GITHUB_OUTPUT": str(out), "BASE_REF": "main", "MAX_CHANGED_LINES": "10",
                   "LARGE_PR": "skip", "EXCLUDE_PATHS": "**/gen/**"}
            cwd = os.getcwd()
            try:
                os.chdir(self.repo)
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(filters.main(), 0)
            finally:
                os.chdir(cwd)
            outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
        self.assertEqual(outputs["changed-lines"], "2")
        self.assertEqual(outputs["skip"], "false")

    def test_main_skips_an_oversized_diff(self):
        with tempfile.TemporaryDirectory() as out_dir:
            out = pathlib.Path(out_dir, "output")
            env = {"GITHUB_OUTPUT": str(out), "BASE_REF": "main", "MAX_CHANGED_LINES": "10", "LARGE_PR": "skip"}
            cwd = os.getcwd()
            try:
                os.chdir(self.repo)
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(filters.main(), 0)
            finally:
                os.chdir(cwd)
            outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
        self.assertEqual((outputs["changed-lines"], outputs["skip"], outputs["note"]), ("52", "true", ""))


if __name__ == "__main__":
    unittest.main()

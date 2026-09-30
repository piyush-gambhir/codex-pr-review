import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import filters  # noqa: E402
import gitrepo  # noqa: E402
import shards  # noqa: E402


def changed(path, old_path="", status="M", added=0, deleted=0, binary=False):
    return shards.ChangedFile(path, old_path, status, added, deleted, binary)


class PlanTest(unittest.TestCase):
    """Shards of about the asked-for size, with directories kept together."""

    def test_a_directory_stays_in_one_shard(self):
        files = [changed("src/a.ts", added=40), changed("src/b.ts", added=40),
                 changed("web/c.ts", added=40), changed("web/d.ts", added=40)]
        plan = shards.plan(files, shard_lines=100)
        self.assertEqual([shard.paths for shard in plan],
                         [["src/a.ts", "src/b.ts"], ["web/c.ts", "web/d.ts"]])
        self.assertEqual([shard.name() for shard in plan], ["src", "web"])

    def test_a_directory_bigger_than_a_shard_is_split_by_file(self):
        files = [changed("src/a.ts", added=90), changed("src/b.ts", added=90),
                 changed("src/c.ts", added=10)]
        plan = shards.plan(files, shard_lines=100)
        self.assertEqual([shard.paths for shard in plan],
                         [["src/a.ts"], ["src/b.ts", "src/c.ts"]])

    def test_a_file_is_never_split_however_big_it_is(self):
        files = [changed("src/huge.ts", added=9000), changed("src/small.ts", added=5)]
        plan = shards.plan(files, shard_lines=100)
        self.assertEqual([shard.paths for shard in plan], [["src/huge.ts"], ["src/small.ts"]])
        self.assertEqual(sum(len(shard.files) for shard in plan), 2)

    def test_tests_are_shardable_on_their_own(self):
        files = [changed("src/a.ts", added=50), changed("src/a.test.ts", added=50),
                 changed("tests/test_a.py", added=50)]
        plan = shards.plan(files, shard_lines=200)
        self.assertEqual([shard.paths for shard in plan],
                         [["src/a.ts"], ["src/a.test.ts", "tests/test_a.py"]])
        plan = shards.plan(files, shard_lines=200, separate_tests=False)
        self.assertEqual(len(plan), 1)

    def test_the_plan_is_deterministic(self):
        files = [changed("b/x.ts", added=30), changed("a/y.ts", added=30),
                 changed("a/x.ts", added=30), changed("c/z.ts", added=30)]
        first = [shard.paths for shard in shards.plan(files, shard_lines=60)]
        second = [shard.paths for shard in shards.plan(list(reversed(files)), shard_lines=60)]
        self.assertEqual(first, second)
        self.assertEqual(first, [["a/x.ts", "a/y.ts"], ["b/x.ts", "c/z.ts"]])

    def test_too_many_shards_grow_instead_of_multiplying_passes(self):
        files = [changed("src/f%02d.ts" % index, added=100) for index in range(40)]
        plan = shards.plan(files, shard_lines=100, max_shards=5)
        self.assertLessEqual(len(plan), 5)
        self.assertEqual(sum(len(shard.files) for shard in plan), 40)

    def test_nothing_changed_plans_nothing(self):
        self.assertEqual(shards.plan([], shard_lines=100), [])

    def test_binary_files_are_carried_even_though_they_count_no_lines(self):
        files = [changed("assets/logo.png", added=0, deleted=0, binary=True)]
        plan = shards.plan(files, shard_lines=100)
        self.assertEqual([shard.paths for shard in plan], [["assets/logo.png"]])
        self.assertEqual(plan[0].lines, 0)


class SyntheticBaseTest(unittest.TestCase):
    """`git diff <synthetic base> <pass head>` has to be the shard, exactly."""

    @classmethod
    def setUpClass(cls):
        cls.repo = gitrepo.Repo()
        cls.indexes = tempfile.TemporaryDirectory()
        repo = cls.repo
        repo.write("src/app.ts", "one\ntwo\n")
        repo.write("src/gone.ts", "removed later\n")
        repo.write("src/old.ts", "x = 1\ny = 2\nz = 3\nw = 4\n")
        repo.write("assets/logo.bin", b"\x00\x01\x02binary\x00")
        repo.write("keep.md", "untouched\n")
        cls.base = repo.commit("base")

        repo.write("src/app.ts", "one\ntwo\nthree\n")
        repo.remove("src/gone.ts")
        repo.move("src/old.ts", "src/new.ts")
        repo.write("src/added.ts", "brand new\n")
        repo.write("assets/logo.bin", b"\x00\x01\x02binary\x00changed")
        cls.head = repo.commit("head")
        cls.files = shards.changed_files(cls.base, cls.head, str(repo.path))
        cls.by_path = {item.path: item for item in cls.files}

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        cls.indexes.cleanup()

    def isolate(self, paths):
        """(base, head) commits for a shard holding exactly `paths`."""
        chosen = [self.by_path[path] for path in paths]
        name = "index-" + "-".join(path.replace("/", "_") for path in paths)
        # Outside the repository, so the repository's own index is never touched.
        index = str(pathlib.Path(self.indexes.name) / name)
        return shards.synthetic_base(chosen, self.base, self.head, str(self.repo.path), index)

    def assert_shard(self, paths, expected):
        base, head = self.isolate(paths)
        self.assertEqual(shards.shard_diff(base, head, str(self.repo.path)), sorted(expected))

    def test_the_diff_is_read_the_way_git_reports_it(self):
        self.assertEqual(sorted(self.by_path), [
            "assets/logo.bin", "src/added.ts", "src/app.ts", "src/gone.ts", "src/new.ts"])
        self.assertEqual(self.by_path["src/added.ts"].status, "A")
        self.assertEqual(self.by_path["src/added.ts"].old_path, "")
        self.assertEqual(self.by_path["src/gone.ts"].status, "D")
        self.assertEqual(self.by_path["src/new.ts"].status, "R")
        self.assertEqual(self.by_path["src/new.ts"].old_path, "src/old.ts")
        self.assertTrue(self.by_path["assets/logo.bin"].binary)
        self.assertEqual(self.by_path["assets/logo.bin"].lines, 0)
        self.assertEqual(self.by_path["src/app.ts"].added, 1)

    def test_a_modified_file_on_its_own(self):
        self.assert_shard(["src/app.ts"], ["src/app.ts"])

    def test_an_added_file_on_its_own(self):
        self.assert_shard(["src/added.ts"], ["src/added.ts"])

    def test_a_deleted_file_on_its_own(self):
        self.assert_shard(["src/gone.ts"], ["src/gone.ts"])

    def test_a_rename_carries_both_of_its_paths(self):
        self.assert_shard(["src/new.ts"], ["src/new.ts", "src/old.ts"])

    def test_a_binary_file_on_its_own(self):
        self.assert_shard(["assets/logo.bin"], ["assets/logo.bin"])

    def test_several_files_together(self):
        self.assert_shard(["src/app.ts", "src/added.ts"], ["src/app.ts", "src/added.ts"])

    def test_every_shard_of_a_plan_adds_up_to_the_whole_diff(self):
        plan = shards.plan(self.files, shard_lines=1)
        covered = []
        for shard in plan:
            base, head = self.isolate(shard.paths)
            covered.extend(shards.shard_diff(base, head, str(self.repo.path)))
        # Every changed path is in exactly one shard's diff (a rename adds its
        # old path, which is not a changed path of its own).
        self.assertEqual(sorted(set(covered) & set(self.by_path)), sorted(self.by_path))
        self.assertEqual(len(covered), len(set(covered)))

    def test_the_pass_head_is_the_pull_request_head_tree(self):
        _, head = self.isolate(["src/app.ts"])
        self.assertEqual(shards.git(["rev-parse", head + "^{tree}"], str(self.repo.path)).strip(),
                         shards.git(["rev-parse", self.head + "^{tree}"], str(self.repo.path)).strip())

    def test_the_base_commit_sits_on_the_merge_base(self):
        base, head = self.isolate(["src/app.ts"])
        self.assertEqual(shards.git(["rev-parse", base + "^"], str(self.repo.path)).strip(), self.base)
        # Which is what makes `--base` mean the shard: the reviewer diffs from
        # the merge base of the two, and that is the synthetic commit itself.
        self.assertEqual(shards.git(["merge-base", base, head], str(self.repo.path)).strip(), base)

    def test_building_them_touches_no_ref_and_no_working_tree(self):
        before_refs, before_head = self.repo.refs(), self.repo.head()
        self.isolate(["src/app.ts", "src/gone.ts"])
        self.assertEqual(self.repo.refs(), before_refs)
        self.assertEqual(self.repo.head(), before_head)
        self.assertEqual(self.repo.dirty(), "")

    def test_the_same_shard_builds_the_same_commits_twice(self):
        self.assertEqual(self.isolate(["src/app.ts"]), self.isolate(["src/app.ts"]))

    def test_path_filters_decide_what_a_plan_has_to_cover(self):
        only = filters.PathFilter(include=["src/**"], exclude=["**/added.ts"])
        kept = shards.changed_files(self.base, self.head, str(self.repo.path), only)
        self.assertEqual(sorted(item.path for item in kept),
                         ["src/app.ts", "src/gone.ts", "src/new.ts"])


if __name__ == "__main__":
    unittest.main()

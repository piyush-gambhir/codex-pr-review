#!/usr/bin/env python3
"""Cut a pull request into shards, and give each shard the whole PR as context.

One `codex exec review` pass samples what looks risky: on a 48,000-line pull
request it reads a few areas and reports what it finds there. A full review
instead reviews every changed file, a shard at a time, and measures what was
actually read (see coverage.py).

The trick is the base ref each pass is given. For shard S:

    B(S) = HEAD's tree, with only S's files put back to their merge-base
           versions (files S adds are removed, files S deletes are restored,
           renames are undone), committed on top of the merge base;
    H(S) = HEAD's tree exactly, committed on top of B(S).

`git diff B(S) H(S)` is then precisely S's changes, while every other file in
the pull request is present at its final state, so Codex reads real callers and
real call sites rather than a slice of a diff. H(S) is needed because
`codex exec review --base <ref>` reviews `merge-base(<ref>, HEAD)..HEAD`: a
sibling commit would fall back to the whole pull request, a commit that H(S)
descends from cannot.

Both commits are written with plumbing (`read-tree`, `update-index`,
`write-tree`, `commit-tree`) against a temporary index file, so nothing touches
the working tree, the repository's own index or any ref. They are reached by
SHA alone and are collected by `git gc` once the runner is gone.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import filters  # noqa: E402

# Identity for the synthetic commits, so they never depend on the runner's git
# config and two runs of the same shard produce the same SHA.
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "codex-pr-review",
    "GIT_AUTHOR_EMAIL": "codex-pr-review@localhost",
    "GIT_COMMITTER_NAME": "codex-pr-review",
    "GIT_COMMITTER_EMAIL": "codex-pr-review@localhost",
    "GIT_AUTHOR_DATE": "1000000000 +0000",
    "GIT_COMMITTER_DATE": "1000000000 +0000",
}
# `git ls-tree` takes the paths on the command line, so long shards are asked in
# batches rather than risking the system's argument limit.
BATCH = 200
# Path segments and file names that mark a test file. Tests are shardable on
# their own so they never crowd the source files out of a shard.
TEST_SEGMENTS = frozenset(("test", "tests", "spec", "specs", "__tests__", "__mocks__", "testdata"))
TEST_MARKERS = ("test", "tests", "spec")


class GitError(RuntimeError):
    """A git command that has to work did not."""


class ChangedFile:
    """One file in the pull request's diff, with what it cost in lines."""

    def __init__(self, path: str, old_path: str = "", status: str = "M",
                 added: int = 0, deleted: int = 0, binary: bool = False):
        self.path = path
        # Where the file was at the merge base; "" when the pull request adds it.
        self.old_path = old_path
        self.status = status
        self.added = added
        self.deleted = deleted
        self.binary = binary

    @property
    def lines(self) -> int:
        return self.added + self.deleted

    @property
    def directory(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    def is_test(self) -> bool:
        """`tests/a.py`, `a.test.ts`, `test_a.py`, `AppSpec.kt` and the like."""
        parts = self.path.lower().split("/")
        if TEST_SEGMENTS & set(parts[:-1]):
            return True
        # Every dotted part of the name except the extension, split on the usual
        # separators: `a.test.ts` and `test_a.py` both say so in a different place.
        name = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
        words = [word for dotted in name.split(".")
                 for chunk in dotted.split("-") for word in chunk.split("_")]
        return bool(set(words) & set(TEST_MARKERS))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ChangedFile(%r, %r, %s, +%d-%d)" % (
            self.path, self.old_path, self.status, self.added, self.deleted)


class Shard:
    """A group of changed files reviewed together in one pass."""

    def __init__(self, index: int, files: list, kind: str = "shard"):
        self.index = index
        self.files = list(files)
        self.kind = kind

    @property
    def paths(self) -> list:
        return [f.path for f in self.files]

    @property
    def lines(self) -> int:
        return sum(f.lines for f in self.files)

    def name(self) -> str:
        """A short label for the shard: its common directory, or the file count."""
        directories = sorted({f.directory for f in self.files})
        if len(directories) == 1:
            return directories[0] or "(root)"
        common = os.path.commonprefix(directories).rsplit("/", 1)[0]
        return (common + "/*") if common else "%d files" % len(self.files)


# Running git ------------------------------------------------------------------


def git(args: list, cwd: str = ".", stdin: str = "", env: dict | None = None) -> str:
    """Run a git command that has to succeed, returning its stdout."""
    environment = dict(os.environ, **(env or {})) if env else None
    try:
        done = subprocess.run(
            ["git"] + list(args), cwd=cwd, env=environment, input=stdin if stdin else None,
            capture_output=True, text=True, check=False)
    except OSError as error:
        raise GitError("could not run git %s: %s" % (" ".join(args), error))
    if done.returncode != 0:
        raise GitError("git %s failed: %s" % (" ".join(args[:3]), done.stderr.strip()[:300]))
    return done.stdout


def merge_base(base_ref: str, cwd: str = ".") -> str:
    """The commit `codex exec review --base <base_ref>` compares against."""
    found = git(["merge-base", base_ref, "HEAD"], cwd).strip()
    return found.splitlines()[0] if found else git(["rev-parse", base_ref], cwd).strip()


# Reading the diff -------------------------------------------------------------


def _numstat(base: str, head: str, cwd: str) -> dict:
    """{path at HEAD: (added, deleted, binary)} from `git diff --numstat -M`."""
    tokens = git(["diff", "--numstat", "-M", "-z", base, head], cwd).split("\0")
    counts, index = {}, 0
    while index < len(tokens):
        record = tokens[index]
        if not record:
            index += 1
            continue
        fields = record.split("\t")
        if len(fields) < 2:
            index += 1
            continue
        added, deleted = fields[0], fields[1]
        if len(fields) >= 3 and fields[2]:
            path, index = fields[2], index + 1
        else:  # a rename: the two paths follow as their own records
            path, index = tokens[index + 2], index + 3
        binary = added == "-" or deleted == "-"
        counts[path] = (0 if binary else int(added), 0 if binary else int(deleted), binary)
    return counts


def _name_status(base: str, head: str, cwd: str) -> list:
    """[(status, old path, new path)] from `git diff --name-status -M`."""
    tokens = git(["diff", "--name-status", "-M", "-z", base, head], cwd).split("\0")
    rows, index = [], 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            index += 1
            continue
        if status[0] in ("R", "C"):
            rows.append((status[0], tokens[index + 1], tokens[index + 2]))
            index += 3
        else:
            rows.append((status[0], tokens[index + 1], tokens[index + 1]))
            index += 2
    return rows


def changed_files(base: str, head: str = "HEAD", cwd: str = ".",
                  paths: filters.PathFilter | None = None) -> list:
    """Every file the pull request changes, in a deterministic order.

    Paths the include/exclude filters drop are left out, so a shard plan covers
    exactly the files a finding could be reported in.
    """
    counts = _numstat(base, head, cwd)
    files = []
    for status, old_path, new_path in _name_status(base, head, cwd):
        added, deleted, binary = counts.get(new_path, (0, 0, False))
        if paths is not None and not paths.allows(new_path):
            continue
        files.append(ChangedFile(
            path=new_path,
            old_path="" if status == "A" else old_path,
            status=status,
            added=added,
            deleted=deleted,
            binary=binary,
        ))
    files.sort(key=lambda f: f.path)
    return files


# Planning ---------------------------------------------------------------------


def _group_by_directory(files: list) -> list:
    """[(directory, [files])] in path order, so a shard plan is deterministic."""
    groups: dict = {}
    for item in files:
        groups.setdefault(item.directory, []).append(item)
    return [(directory, groups[directory]) for directory in sorted(groups)]


def _pack(files: list, limit: int) -> list:
    """Fill shards of about `limit` changed lines, keeping directories together.

    A directory only spills into shards of its own when it is bigger than the
    limit on its own, and a file is never split.
    """
    shards, current, current_lines = [], [], 0

    def close():
        if current:
            shards.append(list(current))
            del current[:]

    for _, group in _group_by_directory(files):
        group_lines = sum(item.lines for item in group)
        if current and current_lines + group_lines > limit:
            close()
            current_lines = 0
        if group_lines > limit:
            for item in group:
                if current and current_lines + item.lines > limit:
                    close()
                    current_lines = 0
                current.append(item)
                current_lines += item.lines
        else:
            current.extend(group)
            current_lines += group_lines
    close()
    return shards


def plan(files: list, shard_lines: int = 2500, max_shards: int = 24,
         separate_tests: bool = True) -> list:
    """Group the changed files into shards of about `shard_lines` changed lines.

    Tests are packed after the source files they belong with rather than mixed
    into the same shard, and the plan is grown until it fits `max_shards`, so a
    very large pull request costs a bounded number of model calls.
    """
    if not files:
        return []
    limit = max(int(shard_lines), 1)
    # Sorted here as well as in changed_files, so a plan never depends on the
    # order the caller happened to hold the files in.
    files = sorted(files, key=lambda item: item.path)
    source = [item for item in files if not (separate_tests and item.is_test())]
    tests = [item for item in files if separate_tests and item.is_test()]
    while True:
        groups = _pack(source, limit) + _pack(tests, limit)
        if max_shards <= 0 or len(groups) <= max_shards:
            break
        # Too many passes: grow the shards until the plan fits the budget.
        grown = max(limit + 1, (sum(item.lines for item in files) // max_shards) + 1)
        limit = grown if grown > limit else limit * 2
    return [Shard(index, group) for index, group in enumerate(groups)]


# Synthetic bases --------------------------------------------------------------


def _ls_tree(commit: str, paths: list, cwd: str) -> dict:
    """{path: (mode, blob sha)} for the paths that exist in that commit."""
    found = {}
    for start in range(0, len(paths), BATCH):
        batch = paths[start:start + BATCH]
        out = git(["ls-tree", "--full-tree", "-z", commit, "--"] + batch, cwd)
        for record in out.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            fields = meta.split()
            if len(fields) >= 3 and fields[1] == "blob":
                found[path] = (fields[0], fields[2])
    return found


def synthetic_base(files: list, base: str, head: str, cwd: str, index_file: str,
                   label: str = "shard") -> tuple:
    """(base commit, head commit) that isolate `files` against the full PR head.

    `git diff <base commit> <head commit>` is exactly the changes to `files`;
    the head commit carries HEAD's tree unchanged, so a checkout of it is the
    whole pull request. Neither commit is named by a ref.
    """
    environment = {"GIT_INDEX_FILE": index_file}
    if os.path.exists(index_file):
        os.remove(index_file)
    git(["read-tree", head], cwd, env=environment)

    at_base = _ls_tree(base, sorted({f.old_path for f in files if f.old_path}), cwd)
    entries = []
    for path, (mode, blob) in sorted(at_base.items()):
        entries.append("%s %s\t%s" % (mode, blob, path))
    restored = set(at_base)
    removed = set()
    for item in files:
        # Anything the shard puts at HEAD that the merge base does not have.
        for path in (item.path, item.old_path):
            if path and path not in restored and path not in removed:
                entries.append("0 %s\t%s" % ("0" * 40, path))
                removed.add(path)
    if entries:
        git(["update-index", "-z", "--index-info"], cwd,
            stdin="\0".join(entries) + "\0", env=environment)
    tree = git(["write-tree"], cwd, env=environment).strip()

    message = "codex-pr-review: base for %s" % label
    base_commit = git(["commit-tree", tree, "-p", base, "-m", message], cwd,
                      env=COMMIT_ENV).strip()
    head_tree = git(["rev-parse", head + "^{tree}"], cwd).strip()
    head_commit = git(["commit-tree", head_tree, "-p", base_commit,
                       "-m", "codex-pr-review: head for %s" % label], cwd,
                      env=COMMIT_ENV).strip()
    return base_commit, head_commit


def shard_diff(base_commit: str, head_commit: str, cwd: str) -> list:
    """The paths `git diff` reports between the two commits, for verification.

    Rename detection is off here, so a rename shows as both of its paths: what
    is being checked is the content the pass is shown, not how Git summarises it.
    """
    out = git(["diff", "--name-only", "--no-renames", "-z", base_commit, head_commit], cwd)
    return sorted(path for path in out.split("\0") if path)

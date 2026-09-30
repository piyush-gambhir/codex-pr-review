#!/usr/bin/env python3
"""What a review covers: which paths count, and whether the diff is too big.

Two independent pieces, both driven by the action's inputs:

* `include-paths` and `exclude-paths`, glob patterns that publish_review.py
  applies to every parsed finding so results outside the paths the caller cares
  about are dropped;
* the `max-changed-lines` guard, which counts added plus deleted lines between
  the merge base of `base-ref` and HEAD (skipping paths the filter excludes) and
  decides whether to skip the review altogether, or to review it shard by shard
  (`review-mode`, see full_review.py).

Run as a script it performs the guard from the pull request checkout and writes
the `changed-lines`, `skip`, `note` and `review-mode` step outputs. Standard
library only.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys

# Path globs -------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def compile_pattern(pattern: str) -> re.Pattern:
    """Translate one glob into a full-match regex.

    fnmatch semantics for `*`, `?` and `[...]`, except that none of them cross a
    directory separator, plus `**` for whole path segments: `src/**/*.ts` matches
    `src/a.ts` as well as `src/a/b.ts`, and `**/gen/**` matches `gen/x` and
    `pkg/gen/x`.
    """
    out, index, size = [], 0, len(pattern)
    while index < size:
        char = pattern[index]
        if pattern[index:index + 3] == "**/":
            # Zero or more leading segments, so the pattern also matches at the root.
            out.append("(?:[^/]*/)*")
            index += 3
        elif pattern[index:index + 2] == "**":
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = pattern.find("]", index + 2)
            if end < 0:
                out.append(re.escape(char))
                index += 1
            else:
                body = pattern[index + 1:end].replace("\\", "\\\\")
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("".join(out) + r"\Z")


def normalise(path: str) -> str:
    """Repository-relative form: no `./` prefix, no leading slash, no spaces."""
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def matches(path: str, pattern: str) -> bool:
    """Does one glob match this path? Case sensitive, like Git's own paths."""
    path, pattern = normalise(path), normalise(pattern)
    if not pattern or not path:
        return False
    regex = compile_pattern(pattern)
    if regex.match(path):
        return True
    # A pattern with no separator also matches the file name anywhere in the
    # tree, so `*.ts` covers `src/app/a.ts` the way .gitignore does.
    return "/" not in pattern and bool(regex.match(path.rsplit("/", 1)[-1]))


def parse_patterns(value: str) -> list[str]:
    """Split a newline or comma separated list of globs, skipping blanks and comments."""
    patterns = []
    for chunk in (value or "").replace(",", "\n").splitlines():
        chunk = chunk.strip().strip("'\"").strip()
        if chunk and not chunk.startswith("#"):
            patterns.append(chunk)
    return patterns


class PathFilter:
    """The include-paths and exclude-paths inputs, applied to one path at a time."""

    def __init__(self, include: list[str] | None = None, exclude: list[str] | None = None):
        self.include = list(include or [])
        self.exclude = list(exclude or [])

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PathFilter":
        return cls(parse_patterns(env.get("INCLUDE_PATHS", "")), parse_patterns(env.get("EXCLUDE_PATHS", "")))

    def active(self) -> bool:
        return bool(self.include or self.exclude)

    def allows(self, path: str) -> bool:
        """Exclusions win, and an empty include list includes everything."""
        if self.include and not any(matches(path, p) for p in self.include):
            return False
        return not any(matches(path, p) for p in self.exclude)


# Size guard -------------------------------------------------------------------


def git(args: list[str], cwd: str = ".") -> str:
    """Run a read-only Git command, returning empty output instead of failing."""
    try:
        done = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        print(f"::warning::Could not run git {' '.join(args)}: {error}")
        return ""
    if done.returncode != 0:
        print(f"::warning::git {' '.join(args)} failed: {done.stderr.strip()[:200]}")
        return ""
    return done.stdout


def merge_base(base_ref: str, cwd: str = ".") -> str:
    """The commit `codex exec review` diffs against, or base-ref if it can't be found."""
    found = git(["merge-base", base_ref, "HEAD"], cwd).strip()
    return found.splitlines()[0] if found else base_ref


def changed_lines(base_ref: str, cwd: str = ".", paths: PathFilter | None = None) -> int:
    """Added plus deleted lines between the merge base of base-ref and HEAD.

    Renames are counted as an add and a delete (`--no-renames`) so every row
    carries a plain path the filter can be applied to. Binary files count zero.
    """
    if not base_ref:
        return 0
    output = git(["diff", "--numstat", "--no-renames", merge_base(base_ref, cwd), "HEAD"], cwd)
    total = 0
    for row in output.splitlines():
        fields = row.split("\t")
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        if paths is not None and not paths.allows(fields[2]):
            continue
        total += int(fields[0]) + int(fields[1])
    return total


def parse_limit(value: str) -> int | None:
    """max-changed-lines: a positive whole number, or None for no limit."""
    value = (value or "").strip()
    if value in ("", "0", "none", "off"):
        return None
    if not value.isdigit():
        raise SystemExit(f"::error::Invalid max-changed-lines '{value}'; use a positive whole number.")
    return int(value)


def parse_mode(value: str) -> str:
    mode = (value or "").strip().lower() or "warn"
    if mode not in ("skip", "warn", "full"):
        raise SystemExit(f"::error::Invalid large-pr '{value}'; use warn, skip or full.")
    return mode


def guard_decision(changed: int, limit: int | None, mode: str) -> tuple[bool, str]:
    """(skip the review, note to show): what to do about a diff of `changed` lines."""
    if limit is None or changed <= limit:
        return False, ""
    if mode == "skip":
        return True, f"PR too large to review ({changed} changed lines, limit {limit})."
    if mode == "full":
        # The full review covers it, so the size is not a caveat; the coverage
        # line under the review says how much of it was actually read.
        return False, ""
    return False, f"Large PR: {changed} changed lines, over the {limit} line limit, so this review may be incomplete."


# Review mode ------------------------------------------------------------------

REVIEW_MODES = ("single", "full", "auto")


def parse_review_mode(value: str) -> str:
    """review-mode: `single`, `full` or `auto` (empty means `single`)."""
    mode = (value or "").strip().lower() or "single"
    if mode not in REVIEW_MODES:
        raise SystemExit(f"::error::Invalid review-mode '{value}'; use single, full or auto.")
    return mode


def resolve_review_mode(review_mode: str, large_pr: str, changed: int, limit: int | None) -> str:
    """`single` or `full`: what this run actually does.

    `full` is asked for outright, or reached by `auto` (and by `large-pr: full`,
    which says the same thing from the size guard's side) once the diff is over
    `max-changed-lines`. Without a limit there is no size to be over, so `auto`
    stays single.
    """
    if review_mode == "full":
        return "full"
    oversized = limit is not None and changed > limit
    if oversized and (review_mode == "auto" or large_pr == "full"):
        return "full"
    return "single"


def set_output(key: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"{key}={value}\n")


def main() -> int:
    env = os.environ
    limit = parse_limit(env.get("MAX_CHANGED_LINES", ""))
    mode = parse_mode(env.get("LARGE_PR", ""))
    review_mode = parse_review_mode(env.get("REVIEW_MODE", ""))
    paths = PathFilter.from_env(dict(env))
    changed = changed_lines(env.get("BASE_REF", "").strip(), ".", paths)
    skip, note = guard_decision(changed, limit, mode)
    resolved = resolve_review_mode(review_mode, mode, changed, limit)
    print(f"Changed lines: {changed}" + (f", limit {limit} ({mode})" if limit else ", no limit"))
    print(f"Review mode: {resolved}" + (f" (asked for {review_mode})" if review_mode != resolved else ""))
    if note:
        print(f"::notice::{note}")
    set_output("changed-lines", str(changed))
    set_output("skip", "true" if skip else "false")
    set_output("note", note if not skip else "")
    set_output("review-mode", resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())

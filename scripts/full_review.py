#!/usr/bin/env python3
"""A review that covers the whole pull request, and says how much it covered.

One `codex exec review` pass samples: it reads what looks risky and stops. This
runs one pass per shard of the diff (see shards.py), each in a checkout of the
full pull request head with a synthetic base that isolates that shard, so every
pass has the whole repository for context and a diff small enough to read end to
end. Passes run concurrently, each with its own `CODEX_HOME` so their session
rollouts never collide.

After the shard passes:

* coverage.py reads every rollout and works out which changed files Codex
  actually looked at. Anything it never opened gets one follow-up pass of its
  own, so a file is not left unreviewed because a pass ran out of interest;
* a final cross-cutting pass reviews the whole diff with the per-area summaries
  in its instructions, and is asked only for what a single area cannot show: an
  API change against its callers, wiring, auth applied unevenly, a migration
  against the code that reads those columns;
* the findings of every pass are merged, de-duplicated on the fingerprint
  publish_review.py already uses, and written back out in Codex's own review
  layout, so publishing a full review and publishing a single one are the same
  code path.

A pass that times out or fails is reported as uncovered rather than dropped: the
coverage file says what was not read, and the review says so too.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import coverage  # noqa: E402
import filters  # noqa: E402
import history  # noqa: E402
import publish_review  # noqa: E402
import shards  # noqa: E402
import suggestions  # noqa: E402

DEV_KEY = "developer_instructions = "
# What each shard pass is told about the slice it was given.
SHARD_NOTE = (
    "This review covers one part of a larger pull request: the diff you are "
    "shown is limited to these files:\n{files}\n\n"
    "Every other file the pull request changes is already present in the "
    "working tree at its final state, so read whatever you need for context. "
    "Report findings only in the files listed above; another pass covers the "
    "rest, and a final pass covers issues that span areas."
)
# What the cross-cutting pass is told, so it does not repeat the shard passes.
CROSS_NOTE = (
    "Every part of this pull request has already been reviewed on its own, with "
    "the whole repository available for context. Those reviews covered:\n{areas}\n\n"
    "Report only issues that span more than one of those areas and that a "
    "review of a single area could not see: a changed function, API or data "
    "contract against the callers that were not changed with it, wiring and "
    "registration of new code, authentication or authorisation applied to some "
    "paths but not others, database migrations against the code that reads or "
    "writes those columns, configuration and feature flags against their uses, "
    "and two implementations of the same behaviour that now disagree. Do not "
    "repeat an issue that is contained in one file or one area."
)
FOLLOWUP_NOTE = (
    "These files are part of the pull request but were not read in the earlier "
    "passes. Review them in full."
)
# Codex writes its findings under this heading; publish_review.py reads it back.
FINDINGS_HEADING = "Full review comments:"


class Pass:
    """One `codex exec review` call: what it reviews, and how it went."""

    def __init__(self, identifier: str, kind: str, label: str, files: list,
                 instructions: str = ""):
        self.id = identifier
        self.kind = kind
        self.label = label
        self.files = list(files)
        self.instructions = instructions
        self.base = ""
        self.commit = ""  # the commit to check out, empty for the PR checkout
        self.cwd = ""
        self.home = ""
        self.review_file = ""
        self.events_file = ""
        self.status = "pending"
        self.seconds = 0.0
        self.summary = ""
        self.findings: list = []
        self.error = ""

    @property
    def paths(self) -> list:
        return [item.path for item in self.files]

    def ok(self) -> bool:
        return self.status == "ok"


class Settings:
    """Everything the run needs, read once from the environment."""

    def __init__(self, env: dict):
        self.env = env
        self.temp = pathlib.Path(env.get("RUNNER_TEMP") or ".")
        self.workspace = os.getcwd()
        self.codex = env.get("CODEX_BIN") or str(self.temp / "codex-cli/node_modules/.bin/codex")
        self.home = env.get("CODEX_HOME") or str(self.temp / "codex-home")
        self.base_ref = (env.get("BASE_REF") or "").strip()
        self.review_file = env.get("REVIEW_FILE") or str(self.temp / "codex-review.md")
        self.events_file = env.get("EVENTS_FILE") or str(self.temp / "codex-review-events.jsonl")
        self.coverage_file = env.get("COVERAGE_FILE") or str(self.temp / "codex-review-coverage.json")
        self.homes_file = env.get("CODEX_HOMES_FILE") or str(self.temp / "codex-review-homes.txt")
        # No floor: a tiny shard size is how the end-to-end harness forces real
        # sharding on a small pull request, and max-shards bounds the cost anyway.
        self.shard_lines = number(env.get("SHARD_LINES"), 2500, 1)
        self.max_shards = number(env.get("MAX_SHARDS"), 24, 1)
        self.parallel = number(env.get("MAX_PARALLEL"), 4, 1)
        self.pass_timeout = number(env.get("PASS_TIMEOUT_MINUTES"), 10, 1) * 60
        self.budget = number(env.get("REVIEW_BUDGET_MINUTES"), 20, 1) * 60
        self.paths = filters.PathFilter.from_env(env)


def number(value, default: int, lowest: int = 0) -> int:
    text = (value or "").strip()
    if not text.lstrip("+").isdigit():
        return default
    return max(int(text), lowest)


# Codex homes and instructions -------------------------------------------------


def pass_home(template: str, target: pathlib.Path, extra: str) -> str:
    """A private `CODEX_HOME` for one pass, with its own extra instructions."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if pathlib.Path(template).is_dir():
        shutil.copytree(template, target,
                        ignore=shutil.ignore_patterns("sessions", "log", "logs", "*.lock"))
    else:
        target.mkdir(parents=True)
    config = target / "config.toml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    config.write_text(with_instructions(text, extra), encoding="utf-8")
    return str(target)


def with_instructions(config: str, extra: str) -> str:
    """Add `extra` to the config's developer instructions, keeping valid TOML."""
    if not extra:
        return config if config.endswith("\n") or not config else config + "\n"
    lines = config.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(DEV_KEY):
            try:
                current = json.loads(line[len(DEV_KEY):])
            except ValueError:
                break
            lines[index] = DEV_KEY + json.dumps(current + "\n\n" + extra, ensure_ascii=False)
            return "\n".join(lines) + "\n"
    # A top-level key has to come before the first table.
    at = next((index for index, line in enumerate(lines) if line.startswith("[")), len(lines))
    lines.insert(at, DEV_KEY + json.dumps(extra, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def listed(paths: list, limit: int = 60) -> str:
    shown = ["- %s" % path for path in paths[:limit]]
    if len(paths) > limit:
        shown.append("- ... and %d more" % (len(paths) - limit))
    return "\n".join(shown)


# Worktrees --------------------------------------------------------------------


class Worktrees:
    """A small pool of checkouts of the pull request head, reused across passes.

    Every pass checks out the same tree, so a slot is created once and then only
    has its HEAD moved, which costs nothing: a fresh `git worktree add` per pass
    would copy the whole repository again.
    """

    def __init__(self, repository: str, root: pathlib.Path, size: int):
        self.repository = repository
        self.root = root
        self.slots: queue.Queue = queue.Queue()
        self.made: list = []
        self.lock = threading.Lock()
        for index in range(size):
            self.slots.put(index)

    def take(self, commit: str) -> tuple:
        slot = self.slots.get()
        path = self.root / ("slot-%d" % slot)
        with self.lock:  # `git worktree add` writes shared administrative files
            if str(path) in self.made:
                shards.git(["checkout", "--detach", "--quiet", commit], str(path))
            else:
                if path.exists():
                    shutil.rmtree(str(path))
                path.parent.mkdir(parents=True, exist_ok=True)
                shards.git(["worktree", "add", "--detach", "--quiet", str(path), commit],
                           self.repository)
                self.made.append(str(path))
        return slot, str(path)

    def give_back(self, slot: int) -> None:
        self.slots.put(slot)

    def clean(self) -> None:
        for path in self.made:
            try:
                shards.git(["worktree", "remove", "--force", path], self.repository)
            except shards.GitError as error:
                print("::warning::Could not remove a review worktree: %s" % error)
        try:
            shards.git(["worktree", "prune"], self.repository)
        except shards.GitError:
            pass


# Running one pass -------------------------------------------------------------


def run_codex(job: Pass, settings: Settings, seconds: int) -> None:
    """Run `codex exec review` for one pass, with its own home and time limit."""
    command = [settings.codex, "exec", "review", "--base", job.base,
               "--json", "-o", job.review_file]
    environment = dict(os.environ, CODEX_HOME=job.home)
    started = time.time()
    with open(job.events_file, "w", encoding="utf-8") as events:
        with open(os.devnull, "rb") as nothing:
            process = subprocess.Popen(
                command, cwd=job.cwd, stdin=nothing, stdout=events,
                stderr=subprocess.PIPE, env=environment, text=True,
                start_new_session=True)
            try:
                _, errors = process.communicate(timeout=seconds)
                status = process.returncode
            except subprocess.TimeoutExpired:
                stop(process)
                job.status = "timeout"
                job.error = "no answer within %d minute(s)" % max(seconds // 60, 1)
                job.seconds = time.time() - started
                return
    job.seconds = time.time() - started
    if status != 0 or not pathlib.Path(job.review_file).is_file() \
            or not pathlib.Path(job.review_file).read_text(encoding="utf-8").strip():
        job.status = "failed"
        job.error = ((errors or "").strip().splitlines() or ["exit %d" % status])[-1][:200]
        return
    job.status = "ok"


def stop(process) -> None:
    """End a pass that ran out of time, and the commands it started with it.

    The whole process group goes, because Codex runs the repository's own tools
    while it reviews; the pipes are then drained so nothing is left blocked on a
    reader that has gone away.
    """
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except OSError:
            break
        try:
            process.communicate(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue
    try:
        process.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def prepare(job: Pass, settings: Settings, index_file: str, base: str, head: str) -> None:
    """Give a pass its synthetic base, its home and its output files."""
    run_dir = settings.temp / "codex-review-passes" / job.id
    run_dir.mkdir(parents=True, exist_ok=True)
    job.review_file = str(run_dir / "review.md")
    job.events_file = str(run_dir / "events.jsonl")
    job.home = pass_home(settings.home, settings.temp / "codex-review-homes" / job.id,
                         job.instructions)
    if job.kind == "cross":
        job.base, job.commit, job.cwd = settings.base_ref, "", settings.workspace
        return
    job.base, job.commit = shards.synthetic_base(
        job.files, base, head, settings.workspace, index_file, job.id)


# Merging ----------------------------------------------------------------------


def relative(path: str, roots: list) -> str:
    """A finding's path as the repository sees it, whichever worktree found it."""
    for root in sorted(roots, key=len, reverse=True):
        trimmed = (root or "").strip("/")
        if trimmed and path.startswith(trimmed + "/"):
            return path[len(trimmed) + 1:]
    return path


def collect(job: Pass, roots: list | None = None) -> None:
    """Parse one pass's review message into a summary and findings.

    Nothing is dropped for being outside the pass's own shard: a pass reads the
    whole repository, so a real issue it noticed next door is worth keeping, and
    the merge below folds it into whatever the owning pass said about it.
    """
    text = pathlib.Path(job.review_file).read_text(encoding="utf-8").strip()
    where = [job.cwd, os.path.realpath(job.cwd)] + list(roots or [])
    summary, findings = publish_review.parse_review(text, "")
    job.summary = summary if findings else text
    for finding in findings:
        finding["path"] = relative(finding["path"], where)
        finding["fingerprint"] = history.fingerprint(finding["path"], finding["title"])
        finding["pass"] = job.id
    job.findings = findings


def same_finding(left: dict, right: dict) -> bool:
    """Two findings that are the same issue seen by two passes."""
    if left["fingerprint"] == right["fingerprint"]:
        return True
    if left["path"] != right["path"]:
        return False
    overlap = history.similarity(history.title_tokens(left["title"]),
                                 history.title_tokens(right["title"]))
    return overlap >= history.SIMILAR_ENOUGH and abs(left["start"] - right["start"]) <= 20


def merge(jobs: list) -> list:
    """Every pass's findings, de-duplicated, worst priority first."""
    kept: list = []
    for job in jobs:
        for finding in job.findings:
            match = next((item for item in kept if same_finding(item, finding)), None)
            if match is None:
                kept.append(dict(finding))
                continue
            if finding["priority"] < match["priority"]:
                match["priority"] = finding["priority"]
            if not match.get("suggestion") and finding.get("suggestion"):
                match["suggestion"] = finding["suggestion"]
            if len(finding.get("body") or "") > len(match.get("body") or ""):
                match["body"] = finding["body"]
    kept.sort(key=lambda f: (f["priority"], f["path"], f["start"]))
    return kept


def render(findings: list, jobs: list, report: dict) -> str:
    """The merged review, in the layout publish_review.py already parses."""
    done = [job for job in jobs if job.ok()]
    lines = ["Full review: %d changed file(s) in %d shard(s), %d pass(es)." % (
        report.get("files_total", 0), report.get("shards", 0), len(done))]
    cross = next((job for job in jobs if job.kind == "cross" and job.ok()), None)
    if cross and cross.summary:
        lines += ["", cross.summary.strip()]
    areas = ["- **%s**: %s" % (job.label, one_line(job.summary))
             for job in done if job.kind != "cross" and job.summary]
    if areas:
        lines += ["", "<details>", "<summary>Per-area summaries</summary>", ""] + areas + ["", "</details>"]
    missed = [job for job in jobs if not job.ok()]
    if missed:
        lines += ["", "Not reviewed: " + ", ".join(
            "%s (%s)" % (job.label, job.error or job.status) for job in missed) + "."]
    body = ["\n".join(lines).strip(), "", FINDINGS_HEADING, ""]
    for finding in findings:
        body.append(finding_block(finding))
    return "\n".join(body).rstrip() + "\n"


def one_line(text: str, limit: int = 220) -> str:
    """A summary squeezed onto one line, for the per-area list."""
    flat = re.sub(r"\s+", " ", (text or "").strip())
    return flat[:limit].rstrip() + ("..." if len(flat) > limit else "")


def finding_block(finding: dict) -> str:
    """One finding, written the way Codex writes them."""
    span = str(finding["start"])
    if finding["end"] > finding["start"]:
        span = "%d-%d" % (finding["start"], finding["end"])
    # The separator is the em dash publish_review.py's FINDING_RE looks for.
    head = "- [P%d] %s \u2014 %s:%s" % (
        finding["priority"], finding["title"], finding["path"], span)
    parts = [finding.get("body") or ""]
    if finding.get("suggestion"):
        parts.append(suggestions.github_block(finding["suggestion"]))
    text = "\n\n".join(part for part in parts if part.strip())
    indented = "\n".join(("  " + line) if line.strip() else "" for line in text.splitlines())
    return head + ("\n" + indented if indented.strip() else "") + "\n"


# The run ----------------------------------------------------------------------


def shard_passes(plan: list) -> list:
    """One pass per shard, each told what its slice is and what surrounds it."""
    jobs = []
    for shard in plan:
        note = SHARD_NOTE.format(files=listed(shard.paths))
        jobs.append(Pass("shard-%d" % (shard.index + 1), "shard",
                         "%s (%d files)" % (shard.name(), len(shard.files)),
                         shard.files, note))
    return jobs


def review_all(jobs: list, settings: Settings, trees: Worktrees, deadline: float,
               base: str, head: str, index_root: pathlib.Path) -> None:
    """Run the passes concurrently, inside the run's remaining time."""
    def one(job: Pass) -> None:
        left = int(min(settings.pass_timeout, deadline - time.time()))
        if left <= 30:
            job.status = "skipped"
            job.error = "the review ran out of time before this pass started"
            return
        slot = None
        try:
            prepare(job, settings, str(index_root / ("index-%s" % job.id)), base, head)
            if job.commit:  # a shard pass reviews its own head commit in a worktree
                slot, job.cwd = trees.take(job.commit)
            run_codex(job, settings, left)
        except (shards.GitError, OSError) as error:
            job.status = "failed"
            job.error = str(error)[:200]
        finally:
            if slot is not None:
                trees.give_back(slot)

    workers = max(min(settings.parallel, len(jobs)), 1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, jobs))


def measure(jobs: list, paths: list, roots: list) -> set:
    """The changed files the finished passes actually read.

    A shard pass that reads its whole diff has read its shard, so its own files
    stand in when the command's output was not recorded. The cross-cutting pass
    gets no such credit: its diff is the whole pull request, which is exactly
    the diff that gets truncated, so only what its output shows counts.
    """
    seen: set = set()
    for job in jobs:
        if not job.home or job.status == "skipped":
            continue
        found = coverage.records([str(pathlib.Path(job.home) / "sessions")])
        scope = set(job.paths) if job.kind != "cross" else set()
        seen |= coverage.inspected(found, paths, roots, scope=scope)
    return seen


def write_events(jobs: list, path: str) -> None:
    """Every pass's event stream in one file, for the failure and check steps."""
    with open(path, "w", encoding="utf-8") as out:
        for job in jobs:
            if job.events_file and pathlib.Path(job.events_file).is_file():
                text = pathlib.Path(job.events_file).read_text(encoding="utf-8", errors="replace")
                out.write(text if text.endswith("\n") or not text else text + "\n")


def set_output(key: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write("%s=%s\n" % (key, value))


def main() -> int:
    settings = Settings(dict(os.environ))
    started = time.time()
    deadline = started + settings.budget
    base = shards.merge_base(settings.base_ref or "HEAD", settings.workspace)
    head = shards.git(["rev-parse", "HEAD"], settings.workspace).strip()
    files = shards.changed_files(base, head, settings.workspace, settings.paths)
    paths = [item.path for item in files]
    plan = shards.plan(files, settings.shard_lines, settings.max_shards)
    print("Full review: %d changed file(s), %d shard(s) of about %d changed line(s)."
          % (len(files), len(plan), settings.shard_lines))
    for shard in plan:
        print("  shard %d: %s, %d line(s), %d file(s)"
              % (shard.index + 1, shard.name(), shard.lines, len(shard.files)))

    jobs = shard_passes(plan)
    index_root = settings.temp / "codex-review-index"
    index_root.mkdir(parents=True, exist_ok=True)
    trees = Worktrees(settings.workspace, settings.temp / "codex-review-worktrees",
                      max(min(settings.parallel, max(len(jobs), 1)), 1))
    roots = [settings.workspace, os.path.realpath(settings.workspace),
             settings.env.get("REVIEW_WORKSPACE", "")]

    sharded = len(jobs) > 1
    if not sharded:
        # One shard is the whole pull request, so there is nothing to isolate.
        jobs = [Pass("whole", "cross", "whole pull request", files)]
    try:
        review_all(jobs, settings, trees, deadline, base, head, index_root)
        for job in jobs:
            if job.ok():
                collect(job, roots)
        seen = measure(jobs, paths, roots)

        missing = [item for item in files if item.path not in seen]
        if missing and sharded and time.time() < deadline:
            print("Follow-up pass for %d file(s) no pass read." % len(missing))
            extra = Pass("followup", "shard", "not yet read (%d files)" % len(missing), missing,
                         FOLLOWUP_NOTE + "\n\n" + SHARD_NOTE.format(
                             files=listed([item.path for item in missing])))
            review_all([extra], settings, trees, deadline, base, head, index_root)
            jobs.append(extra)
            if extra.ok():
                collect(extra, roots)
            seen |= measure([extra], paths, roots)

        if sharded and any(job.ok() for job in jobs) and time.time() < deadline:
            areas = "\n".join("- %s: %s" % (job.label, one_line(job.summary, 160))
                              for job in jobs if job.ok() and job.summary)
            cross = Pass("cross", "cross", "issues that span areas", files,
                         CROSS_NOTE.format(areas=areas or "- the whole pull request"))
            review_all([cross], settings, trees, deadline, base, head, index_root)
            jobs.append(cross)
            if cross.ok():
                collect(cross, roots)
            seen |= measure([cross], paths, roots)
    finally:
        trees.clean()

    done = [job for job in jobs if job.ok()]
    for job in jobs:
        print("  %-10s %-8s %5.0fs %s" % (job.id, job.status, job.seconds,
                                          job.error or "%d finding(s)" % len(job.findings)))
    write_events(jobs, settings.events_file)
    pathlib.Path(settings.homes_file).write_text(
        "\n".join(job.home for job in jobs if job.home) + "\n", encoding="utf-8")

    report = coverage.report("full", paths, seen, len(plan) or (1 if files else 0), len(done))
    coverage.write(settings.coverage_file, report)
    print("Coverage: %d/%d changed file(s) inspected over %d pass(es)."
          % (report["files_inspected"], report["files_total"], report["passes"]))

    if not done:
        print("::error::Every review pass failed; no review to publish.")
        return 1
    findings = merge(jobs)
    pathlib.Path(settings.review_file).write_text(render(findings, jobs, report), encoding="utf-8")
    print("Merged %d finding(s) from %d pass(es) in %.0fs."
          % (len(findings), len(done), time.time() - started))
    set_output("review-file", settings.review_file)
    set_output("passes", str(len(done)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

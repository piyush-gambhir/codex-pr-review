#!/usr/bin/env python3
"""A GitHub check run for one Codex review, with an annotation per finding.

    checks.py start   creates an in-progress check run on the reviewed commit
                      (its id is written to GITHUB_OUTPUT as check-run-id)
    checks.py finish  completes it: conclusion from the findings and
                      fail-on-priority, the issues table as the summary, and
                      one annotation per finding
    checks.py cancel  completes it as cancelled

Like status.py every call is best effort: a missing `checks: write` permission
(403) or any other API error must never fail the review itself.

Note that check runs created with GITHUB_TOKEN attach to the workflow's own
check suite, so the run shows up next to the job rather than as a separate
suite. Standard library only.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import status  # noqa: E402
from publish_review import Context, github, issues_table, parse_priority, plural, set_output, verdict  # noqa: E402

# GitHub accepts at most 50 annotations per request, so the rest go in follow-up
# PATCH calls, and caps the output summary at 65535 characters.
ANNOTATION_BATCH = 50
SUMMARY_LIMIT = 65535
TITLE_LIMIT = 255
MESSAGE_LIMIT = 8000
SUMMARY_NOTE = "\n\n<sub>Summary truncated; the full review is on the pull request.</sub>"
ANNOTATION_LEVEL = {0: "failure", 1: "failure", 2: "warning", 3: "notice"}


# Mapping --------------------------------------------------------------------


def annotation_level(priority: int) -> str:
    return ANNOTATION_LEVEL.get(priority, "notice")


def conclusion(findings: list[dict], fail_on: int | None) -> str:
    """success with no findings, failure when gating trips, neutral otherwise."""
    if not findings:
        return "success"
    if fail_on is not None and min(f["priority"] for f in findings) <= fail_on:
        return "failure"
    return "neutral"


def clamp(text: str, limit: int, note: str = "") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(note)].rstrip() + note


def annotations(findings: list[dict]) -> list[dict]:
    """One annotation per finding, on the lines the finding points at."""
    items = []
    for finding in findings:
        start = max(int(finding["start"]), 1)
        message = (finding.get("body") or "").strip() or finding["title"]
        items.append({
            "path": finding["path"],
            "start_line": start,
            "end_line": max(int(finding["end"]), start),
            "annotation_level": annotation_level(finding["priority"]),
            "title": clamp(f"P{finding['priority']}: {finding['title']}", TITLE_LIMIT),
            "message": clamp(message, MESSAGE_LIMIT),
        })
    return items


def batches(items: list[dict], size: int = ANNOTATION_BATCH) -> list[list[dict]]:
    """Annotation batches; always at least one (possibly empty) request."""
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# Rendering ------------------------------------------------------------------


def headline(findings: list[dict]) -> str:
    """Plain-text verdict for the check run's output title."""
    if not findings:
        return "No issues found"
    counts = ", ".join(
        f"{sum(f['priority'] == p for f in findings)} P{p}" for p in range(4) if any(f["priority"] == p for f in findings)
    )
    return f"{plural(len(findings), 'issue')} ({counts})"


def summary_markdown(prose: str, findings: list[dict], ctx: Context) -> str:
    """The same verdict and issues table as the posted review, for the check output."""
    parts = [verdict(findings), prose.strip()]
    if findings:
        parts.append(issues_table(findings, ctx))
    parts.append(f"<sub>{ctx.meta()}</sub>")
    return clamp("\n\n".join(p for p in parts if p), SUMMARY_LIMIT, SUMMARY_NOTE)


# GitHub API -----------------------------------------------------------------


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call(method: str, path: str, token: str, payload: dict):
    """github() without the exceptions: check runs are never worth failing over."""
    try:
        return github(method, path, token, payload)
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")[:300]
            error.close()
        except (OSError, KeyError, ValueError):  # an error without a readable body
            detail = str(error.reason)
        if error.code == 403:
            print(
                "::warning::Cannot write check runs (403). Grant the job `checks: write`, "
                "or set check-run: false."
            )
        else:
            print(f"::warning::Check run request failed ({error.code}: {detail}).")
        return None
    except (urllib.error.URLError, OSError) as error:
        print(f"::warning::Check run request failed: {error}.")
        return None


def start(repo: str, token: str, ctx: Context) -> None:
    payload = {
        "name": ctx.title,
        "head_sha": ctx.head_sha,
        "status": "in_progress",
        "started_at": now(),
        "output": {"title": "Review in progress", "summary": f"<sub>{ctx.meta('Reviewing')}</sub>"},
    }
    if ctx.run_url:
        payload["details_url"] = ctx.run_url
    check = call("POST", f"/repos/{repo}/check-runs", token, payload)
    if check:
        set_output("check-run-id", str(check["id"]))
        print(f"Check run {check['id']} in progress on {ctx.head_sha[:7]}.")


def complete(repo: str, token: str, check_id: str, result: str, title: str, summary: str,
             items: list[dict] | None = None) -> int:
    """Complete the check run, adding annotations 50 at a time."""
    path = f"/repos/{repo}/check-runs/{check_id}"
    # Every request repeats title and summary: the API requires both whenever
    # output is given, and annotations accumulate across the batches.
    output = {"title": clamp(title, TITLE_LIMIT), "summary": clamp(summary, SUMMARY_LIMIT, SUMMARY_NOTE)}
    completed = {"status": "completed", "conclusion": result, "completed_at": now()}
    sent = 0
    for batch in batches(items or []):
        payload = dict(completed, output=dict(output, annotations=batch))
        if call("PATCH", path, token, payload) is None:
            break
        sent += len(batch)
    print(f"Check run {check_id}: {result}, {plural(sent, 'annotation')}.")
    return sent


# Main -----------------------------------------------------------------------


def load_findings(path: str) -> list[dict] | None:
    """The findings publish_review.py wrote, or None when it never ran."""
    if not path or not pathlib.Path(path).is_file():
        return None
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return None


def read_text(path: str) -> str:
    file = pathlib.Path(path) if path else None
    return file.read_text(encoding="utf-8") if file and file.is_file() else ""


def main(action: str) -> int:
    env = os.environ
    repo, token = env["GITHUB_REPOSITORY"], env.get("GH_TOKEN", "")
    ctx = Context(dict(env))
    check_id = env.get("CHECK_RUN_ID", "").strip()

    if action == "start":
        start(repo, token, ctx)
        return 0
    if action not in ("finish", "cancel"):
        print(f"::error::Unknown check action '{action}'.")
        return 1
    if not check_id:
        print("::warning::No check run to complete; creating it was skipped or refused.")
        return 0

    if action == "cancel":
        complete(repo, token, check_id, "cancelled", "Review cancelled", f"<sub>{ctx.meta('Reviewing')}</sub>")
        return 0

    findings = load_findings(env.get("FINDINGS_FILE", ""))
    if findings is None:
        # The review or the publish step failed, so there is nothing to report.
        reason = status.failure_reason(env.get("EVENTS_FILE", ""))
        detail = f"\n\n```\n{reason}\n```" if reason else ""
        summary = f"The Codex review did not complete.{detail}\n\n<sub>{ctx.meta('Reviewing')}</sub>"
        complete(repo, token, check_id, "failure", "Review failed", summary)
        return 0

    fail_on = parse_priority(env.get("FAIL_ON_PRIORITY", ""), None)
    summary = summary_markdown(read_text(env.get("SUMMARY_FILE", "")), findings, ctx)
    complete(repo, token, check_id, conclusion(findings, fail_on), headline(findings), summary, annotations(findings))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
    except (urllib.error.URLError, KeyError, OSError) as error:
        print(f"::warning::Could not update the review check run: {error}")
        sys.exit(0)

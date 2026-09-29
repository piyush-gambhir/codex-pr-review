#!/usr/bin/env python3
"""Turn a `codex exec review` final message into a GitHub pull request review.

Codex renders its native review as a summary, then a "Full review comments:"
list where each finding looks like:

    - [P1] Title \u2014 /abs/path/to/file.ts:15-18   (\u2014 is an em dash)
      Explanation, possibly over several lines.

Findings on lines that are part of the PR diff become inline comments; the
rest are listed in the review body. If the message can't be parsed, the raw
text is posted instead so a review is never silently dropped.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
PRIORITY_ICON = {0: "🔴", 1: "🟠", 2: "🟡", 3: "⚪"}
FINDING_RE = re.compile(r"^- \[P(?P<priority>\d)\] (?P<title>.+?) \u2014 (?P<path>\S+?):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def parse_review(text: str, workspace: str) -> tuple[str, list[dict]]:
    """Split Codex's review message into (summary, findings)."""
    summary, _, rest = text.partition("Full review comments:")
    findings: list[dict] = []
    current: dict | None = None
    for line in rest.splitlines():
        match = FINDING_RE.match(line.strip())
        if match:
            path = match["path"]
            prefix = workspace.rstrip("/") + "/"
            if path.startswith(prefix):
                path = path[len(prefix):]
            start = int(match["start"])
            current = {
                "priority": int(match["priority"]),
                "title": match["title"].strip(),
                "path": path.lstrip("/"),
                "start": start,
                "end": int(match["end"] or start),
                "body": [],
            }
            findings.append(current)
        elif current is not None:
            current["body"].append(line.strip())
    for finding in findings:
        finding["body"] = "\n".join(finding["body"]).strip()
    return summary.strip(), findings


def github(method: str, path: str, token: str, payload: dict | None = None):
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
        return json.loads(body) if body else None


def commentable_lines(repo: str, pr: str, token: str) -> dict[str, set[int]]:
    """Right-side line numbers GitHub accepts review comments on, per file."""
    lines: dict[str, set[int]] = {}
    page = 1
    while True:
        files = github("GET", f"/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}", token)
        for entry in files:
            allowed = lines.setdefault(entry["filename"], set())
            new_line = 0
            for row in (entry.get("patch") or "").splitlines():
                hunk = HUNK_RE.match(row)
                if hunk:
                    new_line = int(hunk["start"])
                elif row.startswith("-"):
                    continue
                elif row.startswith("\\"):
                    continue
                else:
                    allowed.add(new_line)
                    new_line += 1
        if len(files) < 100:
            return lines
        page += 1


def finding_markdown(finding: dict, inline: bool) -> str:
    icon = PRIORITY_ICON.get(finding["priority"], "⚪")
    head = f"{icon} **[P{finding['priority']}] {finding['title']}**"
    if not inline:
        span = finding["start"] if finding["start"] == finding["end"] else f"{finding['start']}-{finding['end']}"
        head += f" `{finding['path']}:{span}`"
    return f"{head}\n\n{finding['body']}".strip()


def main() -> int:
    env = os.environ
    text = open(env["REVIEW_FILE"], encoding="utf-8").read().strip()
    if not text:
        print("::error::Codex produced an empty review.")
        return 1

    repo, pr, token = env["GITHUB_REPOSITORY"], env["PR_NUMBER"], env["GH_TOKEN"]
    footer = env.get("REVIEW_FOOTER", "").strip()
    title = env.get("REVIEW_TITLE", "Codex review").strip()
    summary, findings = parse_review(text, env.get("REVIEW_WORKSPACE", env.get("GITHUB_WORKSPACE", "")))
    print(f"Parsed {len(findings)} finding(s).")

    if not findings:
        # No structured findings: post the message as-is (usually "no issues").
        body = f"## {title}\n\n{text}"
        if footer:
            body += f"\n\n{footer}"
        github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body})
        return 0

    allowed = commentable_lines(repo, pr, token)
    inline, listed = [], []
    for finding in sorted(findings, key=lambda f: f["priority"]):
        lines = allowed.get(finding["path"], set())
        (inline if finding["end"] in lines else listed).append(finding)

    def review_body(listed_findings: list[dict]) -> str:
        parts = [f"## {title}", summary]
        if listed_findings:
            parts.append("**Findings outside the diff:**" if inline else "**Findings:**")
            parts.extend(finding_markdown(f, inline=False) for f in listed_findings)
        if footer:
            parts.append(footer)
        return "\n\n".join(p for p in parts if p)

    comments = []
    for finding in inline:
        comment = {"path": finding["path"], "line": finding["end"], "side": "RIGHT", "body": finding_markdown(finding, inline=True)}
        if finding["start"] < finding["end"] and finding["start"] in allowed[finding["path"]]:
            comment.update(start_line=finding["start"], start_side="RIGHT")
        comments.append(comment)

    payload = {"commit_id": env["HEAD_SHA"], "event": "COMMENT", "body": review_body(listed), "comments": comments}
    try:
        github("POST", f"/repos/{repo}/pulls/{pr}/reviews", token, payload)
    except urllib.error.HTTPError as error:
        # A line GitHub won't anchor to rejects the whole review: fall back to
        # listing everything in the body rather than losing the findings.
        print(f"::warning::Inline review rejected ({error.code}: {error.read().decode()[:300]}); posting findings in the body.")
        payload.update(body=review_body(inline + listed), comments=[])
        github("POST", f"/repos/{repo}/pulls/{pr}/reviews", token, payload)
    print(f"Posted review: {len(comments)} inline, {len(listed)} in body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

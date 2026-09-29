#!/usr/bin/env python3
"""Turn a `codex exec review` final message into pull request feedback.

Codex renders its native review as a summary, then a "Full review comments:"
list where each finding looks like:

    - [P1] Title \u2014 /abs/path/to/file.ts:15-18      (\u2014 is an em dash)
      Explanation, possibly over several lines.

Depending on POST_MODE the findings become a pull request review (inline
comments on diff lines, the rest in the body), a single comment, or nothing.
Findings, counts and the highest priority are always written as step outputs
and to the job summary. If the message can't be parsed, the raw text is posted
so a review is never silently dropped.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

import history

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
GRAPHQL = os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
MARKER = "<!-- codex-pr-review -->"
# Every comment this action writes (reviews and status notes) starts with this.
MARKER_PREFIX = "<!-- codex-pr-review"
PRIORITY_ICON = {0: "\U0001f534", 1: "\U0001f7e0", 2: "\U0001f7e1", 3: "\u26aa"}
# Tolerates text before the tag ("- Security [P1] ...") and a relative or
# absolute path, since custom instructions can nudge Codex's layout.
FINDING_RE = re.compile(
    r"^-\s+(?P<pre>.*?)\[P(?P<priority>\d)\]\s+(?P<title>.+?)\s+\u2014\s+"
    r"(?P<path>\S+?):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$"
)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


# Parsing ---------------------------------------------------------------------


def parse_review(text: str, workspace: str) -> tuple[str, list[dict]]:
    """Split Codex's review message into (summary, findings)."""
    summary, _, rest = text.partition("Full review comments:")
    findings: list[dict] = []
    current: dict | None = None
    prefix = workspace.rstrip("/") + "/" if workspace else None
    for line in rest.splitlines():
        match = FINDING_RE.match(line.strip())
        if match:
            path = match["path"]
            if prefix and path.startswith(prefix):
                path = path[len(prefix):]
            start = int(match["start"])
            title = f"{match['pre'].strip()} {match['title'].strip()}".strip()
            current = {
                "priority": int(match["priority"]),
                "title": title,
                "path": path.lstrip("/"),
                "start": start,
                "end": max(int(match["end"] or start), start),
                "body": [],
            }
            findings.append(current)
        elif current is not None:
            current["body"].append(line.strip())
    for finding in findings:
        finding["body"] = "\n".join(finding["body"]).strip()
    return summary.strip(), findings


def parse_priority(value: str, default: int | None) -> int | None:
    """Accept '2', 'P2' or 'p2'; empty or 'none' gives the default."""
    value = (value or "").strip().lower()
    if value in ("", "none", "off"):
        return default
    number = value[1:] if value.startswith("p") else value
    if not number.isdigit() or not 0 <= int(number) <= 3:
        raise SystemExit(f"::error::Invalid priority '{value}'; use P0, P1, P2 or P3.")
    return int(number)


# GitHub API ------------------------------------------------------------------


def github(method: str, path: str, token: str, payload: dict | None = None, url: str | None = None):
    request = urllib.request.Request(
        url or f"{API}{path}",
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


def paginate(path: str, token: str) -> list:
    items, page = [], 1
    joiner = "&" if "?" in path else "?"
    while True:
        batch = github("GET", f"{path}{joiner}per_page=100&page={page}", token)
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def commentable_lines(repo: str, pr: str, token: str) -> dict[str, set[int]]:
    """Right-side line numbers GitHub accepts review comments on, per file."""
    lines: dict[str, set[int]] = {}
    for entry in paginate(f"/repos/{repo}/pulls/{pr}/files", token):
        allowed = lines.setdefault(entry["filename"], set())
        new_line = 0
        for row in (entry.get("patch") or "").splitlines():
            hunk = HUNK_RE.match(row)
            if hunk:
                new_line = int(hunk["start"])
            elif row.startswith(("-", "\\")):
                continue
            else:
                allowed.add(new_line)
                new_line += 1
    return lines


def hide_previous(repo: str, pr: str, token: str, keep_id: str = "") -> int:
    """Collapse earlier Codex comments on the PR as outdated."""
    node_ids = [
        c["node_id"]
        for c in paginate(f"/repos/{repo}/issues/{pr}/comments", token)
        + paginate(f"/repos/{repo}/pulls/{pr}/comments", token)
        if (c.get("body") or "").startswith(MARKER_PREFIX) or MARKER in (c.get("body") or "")
        if str(c["id"]) != keep_id
    ]
    hidden = 0
    for node_id in node_ids:
        query = "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { clientMutationId } }"
        try:
            github("POST", "", token, {"query": query, "variables": {"id": node_id}}, url=GRAPHQL)
            hidden += 1
        except urllib.error.HTTPError as error:
            print(f"::warning::Could not hide an earlier Codex comment ({error.code}).")
    return hidden


# Rendering -------------------------------------------------------------------


class Context:
    """Everything the rendered review links to or mentions."""

    def __init__(self, env: dict[str, str]):
        self.repo = env.get("GITHUB_REPOSITORY", "")
        self.head_sha = env.get("HEAD_SHA", "")
        self.server = env.get("GITHUB_SERVER_URL", "https://github.com")
        self.title = env.get("REVIEW_TITLE", "Codex review").strip() or "Codex review"
        self.base = env.get("BASE_REF", "").strip()
        if self.base.startswith("origin/"):
            self.base = self.base[len("origin/"):]
        self.model = env.get("MODEL", "").strip()
        self.label = env.get("LABEL", "").strip()
        self.effort = env.get("REASONING_EFFORT", "").strip()
        self.tokens = env.get("USAGE_TEXT", "").strip()
        self.run_url = env.get("RUN_URL", "").strip()
        self.rerun_hint = env.get("RERUN_HINT", "").strip()
        self.note = ""
        # Re-review awareness, filled in from the previous review's state.
        self.previous_sha = env.get("PREVIOUS_SHA", "").strip()
        self.scope_note = env.get("INCREMENTAL_NOTE", "").strip()
        self.resolved: list[dict] = []
        self.carried: list[dict] = []
        self.still_open: set[str] = set()
        self.state = ""

    def commit_link(self) -> str:
        short = self.head_sha[:7]
        return f"[`{short}`]({self.server}/{self.repo}/commit/{self.head_sha})" if self.head_sha else ""

    def meta(self, verb: str = "Reviewed") -> str:
        reviewed = f"{verb} {self.commit_link()}" if self.head_sha else verb
        if self.base:
            reviewed += f" against `{self.base}`"
        parts = [reviewed]
        if self.scope_note:
            parts.append(self.scope_note)
        if self.model:
            parts.append(f"`{self.model}`" + (f" via {self.label}" if self.label else ""))
        if self.effort:
            parts.append(f"{self.effort} effort")
        if self.tokens:
            parts.append(self.tokens)
        if self.run_url:
            parts.append(f"[View run]({self.run_url})")
        return " \u00b7 ".join(parts)

    def footer(self) -> str:
        lines = [f"<sub>{self.note}</sub>"] if self.note else []
        lines.append(f"<sub>{self.meta()}</sub>")
        if self.rerun_hint:
            lines.append(f"<sub>{self.rerun_hint}</sub>")
        return "\n".join(lines)


def span(finding: dict) -> str:
    return str(finding["start"]) if finding["start"] == finding["end"] else f"{finding['start']}-{finding['end']}"


def location(finding: dict, ctx: Context) -> str:
    """`path:lines` linked to those lines at the reviewed commit."""
    anchor = f"L{finding['start']}" + (f"-L{finding['end']}" if finding["end"] > finding["start"] else "")
    url = f"{ctx.server}/{ctx.repo}/blob/{ctx.head_sha}/{finding['path']}#{anchor}"
    return f"[`{finding['path']}:{span(finding)}`]({url})"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def verdict(findings: list[dict], resolved: int = 0) -> str:
    fixed = f" \u00b7 {resolved} resolved" if resolved else ""
    if not findings:
        return f"\u2705 **No issues found.**{fixed}"
    counts = ", ".join(
        f"{sum(f['priority'] == p for f in findings)} P{p}" for p in range(4) if any(f["priority"] == p for f in findings)
    )
    worst = min(f["priority"] for f in findings)
    if worst <= 1:
        return f"{PRIORITY_ICON[worst]} **{plural(len(findings), 'issue')} to address** ({counts}){fixed}"
    return f"{PRIORITY_ICON[worst]} **{plural(len(findings), 'minor issue')}** ({counts}){fixed}"


def inline_comment(finding: dict, ctx: Context) -> str:
    icon = PRIORITY_ICON.get(finding["priority"], "\u26aa")
    return (
        f"{icon} **P{finding['priority']} \u00b7 {finding['title']}**\n\n{finding['body']}\n\n"
        f"<sub>{ctx.title} \u00b7 {location(finding, ctx)}</sub>\n"
        f"{history.finding_marker(history.entry(finding)['fp'])}\n{MARKER}"
    )


def details(finding: dict, ctx: Context) -> str:
    """A collapsible block for a finding that has no inline comment."""
    icon = PRIORITY_ICON.get(finding["priority"], "\u26aa")
    summary = f"{icon} <b>P{finding['priority']}</b> \u00b7 {html.escape(finding['title'])} \u00b7 <code>{html.escape(finding['path'])}:{span(finding)}</code>"
    return f"<details>\n<summary>{summary}</summary>\n\n{location(finding, ctx)}\n\n{finding['body']}\n\n</details>"


def issues_table(findings: list[dict], ctx: Context, inline_ids: set[int]) -> str:
    rows = ["| | Priority | Issue | Location | |", "|---|---|---|---|---|"]
    for finding in findings:
        title = finding["title"].replace("|", "\\|")
        if finding.get("fingerprint") in ctx.still_open:
            title += " <sub>(still open)</sub>"
        where = "\U0001f4ac inline" if id(finding) in inline_ids else "\u2b07\ufe0f below"
        icon = PRIORITY_ICON.get(finding["priority"], "\u26aa")
        rows.append(
            f"| {icon} | P{finding['priority']} | {title} "
            f"| {location(finding, ctx)} | {where} |"
        )
    return "\n".join(rows)


def body_markdown(summary: str, findings: list[dict], ctx: Context, inline_ids: set[int] | None = None) -> str:
    """Verdict, summary, the full issues table, collapsible details, then meta and next steps."""
    inline_ids = inline_ids or set()
    parts = [MARKER, f"## {ctx.title}", verdict(findings, len(ctx.resolved)), summary]
    if findings:
        parts.append(issues_table(findings, ctx, inline_ids))
        detailed = [f for f in findings if id(f) not in inline_ids]
        if detailed:
            parts.append("**Details**" if inline_ids else "**Details** (click to expand)")
            parts.extend(details(f, ctx) for f in detailed)
    if ctx.resolved or ctx.carried:
        parts.append(history.resolved_section(ctx.resolved, ctx.carried, ctx))
    parts.append(ctx.footer())
    parts.append(ctx.state)
    return "\n\n".join(p for p in parts if p)


def set_output(key: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"{key}={value}\n")


def write_outputs(findings: list[dict], filtered_out: int, resolved: int = 0) -> None:
    findings_file = pathlib.Path(os.environ.get("RUNNER_TEMP", ".")) / "codex-review-findings.json"
    findings_file.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    highest = f"P{min(f['priority'] for f in findings)}" if findings else ""
    outputs = {
        "findings-count": str(len(findings)),
        "highest-priority": highest,
        "findings-file": str(findings_file),
        "filtered-count": str(filtered_out),
        "resolved-count": str(resolved),
    }
    for key, value in outputs.items():
        set_output(key, value)


def write_summary(summary: str, findings: list[dict], ctx: Context) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            body = history.strip_state(body_markdown(summary, findings, ctx))
            out.write(body.replace(MARKER, "").strip() + "\n")


# Main ------------------------------------------------------------------------


def main() -> int:
    env = os.environ
    text = pathlib.Path(env["REVIEW_FILE"]).read_text(encoding="utf-8").strip()
    if not text:
        print("::error::Codex produced an empty review.")
        return 1

    repo, pr, token = env["GITHUB_REPOSITORY"], env["PR_NUMBER"], env.get("GH_TOKEN", "")
    mode = env.get("POST_MODE", "review").strip().lower()
    if mode not in ("review", "comment", "none"):
        print(f"::error::Unknown post-mode '{mode}'; use review, comment or none.")
        return 1
    ctx = Context(dict(env))
    max_priority = parse_priority(env.get("MAX_PRIORITY", ""), 3)
    fail_on = parse_priority(env.get("FAIL_ON_PRIORITY", ""), None)

    summary, all_findings = parse_review(text, env.get("REVIEW_WORKSPACE", ""))
    if not all_findings:
        summary = text  # unstructured message, usually "no issues"
    for finding in all_findings:
        finding["fingerprint"] = history.fingerprint(finding["path"], finding["title"])
    findings = sorted((f for f in all_findings if f["priority"] <= max_priority), key=lambda f: f["priority"])
    filtered_out = len(all_findings) - len(findings)
    print(f"Parsed {len(all_findings)} finding(s); {len(findings)} at P{max_priority} or above.")
    if filtered_out:
        ctx.note = f"{plural(filtered_out, 'lower-priority finding')} below P{max_priority} not shown."

    # What the previous review reported, so this one can say what got fixed.
    plan = history.load_plan(env.get("STATE_FILE", ""))
    previous = plan.get("previous") or {}
    ctx.previous_sha = ctx.previous_sha or previous.get("sha", "")
    ctx.resolved, ctx.carried, ctx.still_open = history.classify(
        previous.get("findings") or [], findings, plan.get("changed-files")
    )
    ctx.state = history.state_marker(ctx.head_sha, findings, ctx.carried)
    if previous:
        print(f"Since {history.short(ctx.previous_sha)}: {len(ctx.resolved)} resolved, "
              f"{len(ctx.still_open)} still open, {len(ctx.carried)} not re-checked.")

    write_outputs(findings, filtered_out, len(ctx.resolved))
    write_summary(summary, findings, ctx)

    if mode != "none":
        if env.get("HIDE_PREVIOUS", "true").strip().lower() == "true":
            print(f"Hid {hide_previous(repo, pr, token, env.get('STATUS_COMMENT_ID', ''))} earlier Codex comment(s).")
        if mode == "comment" or not findings:
            github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body_markdown(summary, findings, ctx)})
            print(f"Posted comment with {plural(len(findings), 'finding')}.")
        else:
            post_review(repo, pr, token, summary, findings, ctx)
        set_output("posted", "true")
        if ctx.resolved and env.get("RESOLVE_FIXED_THREADS", "true").strip().lower() == "true":
            fixed = {item["fp"] for item in ctx.resolved}
            print(f"Resolved {history.resolve_threads(repo, pr, token, fixed, github, GRAPHQL)} fixed thread(s).")

    if fail_on is not None and findings and min(f["priority"] for f in findings) <= fail_on:
        print(f"::error::Codex found P{min(f['priority'] for f in findings)} issues (fail-on-priority is P{fail_on}).")
        return 1
    return 0


def post_review(repo: str, pr: str, token: str, summary: str, findings: list[dict], ctx: Context) -> None:
    """One review: verdict and full issues table in the body, inline comments on diff lines."""
    allowed = commentable_lines(repo, pr, token)
    inline = [f for f in findings if f["end"] in allowed.get(f["path"], set())]

    comments = []
    for finding in inline:
        comment = {"path": finding["path"], "line": finding["end"], "side": "RIGHT", "body": inline_comment(finding, ctx)}
        if finding["start"] < finding["end"] and finding["start"] in allowed[finding["path"]]:
            comment.update(start_line=finding["start"], start_side="RIGHT")
        comments.append(comment)

    payload = {
        "commit_id": ctx.head_sha,
        "event": "COMMENT",
        "body": body_markdown(summary, findings, ctx, {id(f) for f in inline}),
        "comments": comments,
    }
    try:
        github("POST", f"/repos/{repo}/pulls/{pr}/reviews", token, payload)
    except urllib.error.HTTPError as error:
        # A line GitHub won't anchor to rejects the whole review: fall back to
        # full details in the body rather than losing the findings.
        print(f"::warning::Inline review rejected ({error.code}: {error.read().decode()[:300]}); posting findings in the body.")
        payload.update(body=body_markdown(summary, findings, ctx), comments=[])
        github("POST", f"/repos/{repo}/pulls/{pr}/reviews", token, payload)
        inline = []
    print(f"Posted review: {plural(len(findings), 'issue')}, {len(inline)} inline.")


if __name__ == "__main__":
    sys.exit(main())

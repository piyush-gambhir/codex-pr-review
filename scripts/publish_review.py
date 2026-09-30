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
import http.client
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import coverage  # noqa: E402
import filters  # noqa: E402
import history  # noqa: E402
import icons as icon_set  # noqa: E402
import suggestions  # noqa: E402

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
GRAPHQL = os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
MARKER = "<!-- codex-pr-review -->"
# Every comment this action writes (reviews and status notes) starts with this.
MARKER_PREFIX = "<!-- codex-pr-review"
# GitHub's own alert blocks carry the verdict: it draws the octicon, the colour
# and the border, so the headline looks like the rest of the pull request page.
# The type is the worst priority reported, TIP when there is nothing to report.
VERDICT_ALERT = {0: "CAUTION", 1: "CAUTION", 2: "WARNING", 3: "NOTE"}
CLEAN_ALERT = "TIP"
# Tolerates text before the tag ("- Security [P1] ...") and a relative or
# absolute path, since custom instructions can nudge Codex's layout.
FINDING_RE = re.compile(
    r"^-\s+(?P<pre>.*?)\[P(?P<priority>\d)\]\s+(?P<title>.+?)\s+\u2014\s+"
    r"(?P<path>\S+?):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$"
)
FINDINGS_HEADING_RE = re.compile(r"^(?:Full )?review comments?:[ \t]*$", re.IGNORECASE | re.MULTILINE)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


# Parsing ---------------------------------------------------------------------


def dedent_body(lines: list[str]) -> str:
    """Join a finding's explanation, dropping only the indent Codex adds to all of
    it, so a fenced block inside keeps its own indentation."""
    kept = [line.rstrip() for line in lines]
    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()
    indents = [len(line) - len(line.lstrip()) for line in kept if line]
    cut = min(indents) if indents else 0
    return "\n".join(line[cut:] for line in kept)


def parse_review(text: str, workspace: str) -> tuple[str, list[dict]]:
    """Split Codex's review message into (summary, findings)."""
    # "Full review comments:" normally; "Review comment:" when there is only one.
    heading = FINDINGS_HEADING_RE.search(text)
    summary, rest = (text[: heading.start()], text[heading.end():]) if heading else (text, "")
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
                "suggestion": "",
            }
            findings.append(current)
        elif current is not None:
            current["body"].append(line)
    for finding in findings:
        # A suggestion block is always pulled out, even when the input is off, so
        # a stray one can never reach GitHub as an applicable suggestion.
        finding["body"], finding["suggestion"] = suggestions.extract(dedent_body(finding["body"]))
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


# Transient failures worth another attempt. GitHub drops idle connections and
# answers 5xx or 429 under load; a review shouldn't die on one of those.
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Statuses where GitHub has not acted on the request, so even a POST is safe to repeat.
NOT_PROCESSED = {429, 503}
RETRY_DELAYS = (2, 5, 10)


def is_idempotent(method: str, url: str | None) -> bool:
    """Safe to repeat after an ambiguous failure: reads, and our GraphQL calls
    (minimizing a comment or resolving a thread twice changes nothing)."""
    return method in ("GET", "HEAD", "PUT", "DELETE") or (url or "").endswith("/graphql")


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
    for attempt, delay in enumerate(RETRY_DELAYS + (None,)):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            statuses = RETRY_STATUSES if is_idempotent(method, url) else NOT_PROCESSED
            if delay is None or error.code not in statuses:
                raise
            reason = f"HTTP {error.code}"
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as error:
            # A dropped connection may have reached GitHub, so only repeat what's safe.
            if delay is None or not is_idempotent(method, url):
                raise
            reason = type(error).__name__
        print(f"::warning::GitHub API {method} failed ({reason}); retrying in {delay}s (attempt {attempt + 2}).")
        time.sleep(delay)
    return None


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


def hide_previous(repo: str, pr: str, token: str, keep_id: str = "", known: list | None = None) -> int:
    """Collapse earlier Codex comments on the PR as outdated.

    `known` are the {node_id, id} pairs of this action's earlier comments, read
    once for the whole run (see pr_state.py); without them they are listed here.
    """
    if known is None:
        known = [
            {"node_id": c["node_id"], "id": c["id"]}
            for c in paginate(f"/repos/{repo}/issues/{pr}/comments", token)
            + paginate(f"/repos/{repo}/pulls/{pr}/comments", token)
            if (c.get("body") or "").startswith(MARKER_PREFIX) or MARKER in (c.get("body") or "")
        ]
    node_ids = [c["node_id"] for c in known if str(c.get("id") or "") != keep_id]
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
        if re.fullmatch(r"[0-9a-f]{40}", self.base):
            self.base = self.base[:7]  # an incremental run reviews against a commit
        self.model = env.get("MODEL", "").strip()
        self.label = env.get("LABEL", "").strip()
        self.effort = env.get("REASONING_EFFORT", "").strip()
        self.tokens = env.get("USAGE_TEXT", "").strip()
        # How much of the diff the reviewer actually read (see coverage.py).
        self.coverage = coverage.summary(coverage.load(env.get("COVERAGE_FILE", "").strip()))
        self.run_url = env.get("RUN_URL", "").strip()
        self.rerun_hint = env.get("RERUN_HINT", "").strip()
        self.note = ""
        # Where the inline images come from, and whether there are any at all.
        self.icons = icon_set.Icons.from_env(env)
        # Re-review awareness, filled in from the previous review's state.
        self.previous_sha = env.get("PREVIOUS_SHA", "").strip()
        self.scope_note = env.get("INCREMENTAL_NOTE", "").strip()
        self.resolved: list[dict] = []
        self.carried: list[dict] = []
        self.still_open: set[str] = set()
        self.state = ""
        self.incremental = False

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
        if self.coverage:
            parts.append(self.coverage)
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


def alert(kind: str, body: str) -> str:
    """A GitHub alert block, which GitHub renders with its own icon and colour."""
    lines = [f"> [!{kind}]"]
    lines.extend(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    return "\n".join(lines)


def priority_counts(findings: list[dict]) -> str:
    return ", ".join(
        f"{sum(f['priority'] == p for f in findings)} P{p}" for p in range(4) if any(f["priority"] == p for f in findings)
    )


def verdict(findings: list[dict], resolved: int = 0) -> str:
    """The headline as an alert block: type from the worst priority reported."""
    fixed = f" \u00b7 {resolved} resolved" if resolved else ""
    if not findings:
        return alert(CLEAN_ALERT, f"**No issues found.**{fixed}")
    worst = min(f["priority"] for f in findings)
    scale = "issue" if worst <= 1 else "minor issue"
    verb = " to address" if worst <= 1 else ""
    headline = f"**{plural(len(findings), scale)}{verb}** ({priority_counts(findings)}){fixed}"
    return alert(VERDICT_ALERT.get(worst, "NOTE"), headline)


def suggestion_block(finding: dict, exact_range: bool = True, native: bool = True) -> str:
    """A finding's suggested fix, or "" when it has none.

    GitHub applies a `suggestion` block to the lines the comment is anchored to,
    so it is only passed through when those are exactly the lines the fix
    replaces. Anywhere else it becomes a labelled code block instead.
    """
    code = finding.get("suggestion") or ""
    if not code:
        return ""
    if exact_range and native:
        return suggestions.github_block(code)
    where = f"replaces `{finding['path']}:{span(finding)}`" if not exact_range else ""
    return suggestions.plain_block(code, where)


def inline_comment(finding: dict, ctx: Context, exact_range: bool = True, native: bool = True) -> str:
    icon = ctx.icons.img(icon_set.priority(finding["priority"]))
    fix = suggestion_block(finding, exact_range, native)
    return (
        f"{icon}{' ' if icon else ''}**P{finding['priority']} \u00b7 {finding['title']}**\n\n{finding['body']}\n\n"
        + (f"{fix}\n\n" if fix else "")
        + f"<sub>{ctx.title} \u00b7 {location(finding, ctx)}</sub>\n"
        + f"{history.finding_marker(history.entry(finding)['fp'])}\n{MARKER}"
    )


def details(finding: dict, ctx: Context) -> str:
    """A collapsible block for a finding that has no inline comment."""
    icon = ctx.icons.img(icon_set.priority(finding["priority"]))
    summary = (
        f"{icon}{' ' if icon else ''}<b>P{finding['priority']}</b> \u00b7 {html.escape(finding['title'])}"
        f" \u00b7 <code>{html.escape(finding['path'])}:{span(finding)}</code>"
    )
    # Never a native suggestion here: nothing in the body anchors to a diff line.
    fix = suggestion_block(finding, exact_range=True, native=False)
    parts = [location(finding, ctx), finding["body"]] + ([fix] if fix else [])
    return f"<details>\n<summary>{summary}</summary>\n\n" + "\n\n".join(p for p in parts if p) + "\n\n</details>"


def issues_table(findings: list[dict], ctx: Context, inline_ids: set[int] | None = None) -> str:
    """One row per finding; inline_ids None drops the "Where" column (check runs).

    The leading icon column is dropped as well when images are off, so the table
    never carries a column of blanks.
    """
    graphics = ctx.icons.enabled
    head = (["", "Priority", "Issue", "Location"] if graphics else ["Priority", "Issue", "Location"])
    if inline_ids is not None:
        head.append("Where")
    rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for finding in findings:
        title = finding["title"].replace("|", "\\|")
        if finding.get("fingerprint") in ctx.still_open:
            title += " " + ctx.icons.marker("still-open", "still open")
        if finding.get("suggestion"):
            # Marks the findings that come with a ready-made fix.
            title += " " + ctx.icons.marker("suggestion", "suggested fix")
        cells = [f"P{finding['priority']}", title, location(finding, ctx)]
        if graphics:
            cells.insert(0, ctx.icons.img(icon_set.priority(finding["priority"])))
        if inline_ids is not None:
            inline = id(finding) in inline_ids
            cells.append(ctx.icons.tagged("inline" if inline else "outside", "Inline" if inline else "Below"))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def body_markdown(summary: str, findings: list[dict], ctx: Context, inline_ids: set[int] | None = None) -> str:
    """Verdict, summary, the full issues table, collapsible details, then meta and next steps."""
    inline_ids = inline_ids or set()
    parts = [MARKER, f"## {ctx.title}", verdict(findings, len(ctx.resolved)), summary]
    if findings:
        # With nothing inline (comment mode, or an inline review GitHub rejected)
        # every row would say the same thing, so the column is left out.
        parts.append(issues_table(findings, ctx, inline_ids or None))
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


def write_outputs(findings: list[dict], filtered_out: int, resolved: int = 0, path_filtered_out: int = 0) -> None:
    findings_file = pathlib.Path(os.environ.get("RUNNER_TEMP", ".")) / "codex-review-findings.json"
    findings_file.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    highest = f"P{min(f['priority'] for f in findings)}" if findings else ""
    outputs = {
        "findings-count": str(len(findings)),
        "highest-priority": highest,
        "findings-file": str(findings_file),
        "filtered-count": str(filtered_out),
        "resolved-count": str(resolved),
        "path-filtered-count": str(path_filtered_out),
    }
    for key, value in outputs.items():
        set_output(key, value)


def write_summary_file(summary: str) -> None:
    """Keep Codex's prose summary for later steps, such as the check run output."""
    path = os.environ.get("SUMMARY_FILE", "").strip()
    if path:
        pathlib.Path(path).write_text(summary, encoding="utf-8")


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

    paths = filters.PathFilter.from_env(dict(env))
    native_suggestions = env.get("SUGGESTIONS", "true").strip().lower() != "false"

    summary, all_findings = parse_review(text, env.get("REVIEW_WORKSPACE", ""))
    if not all_findings:
        summary = text  # unstructured message, usually "no issues"
    for finding in all_findings:
        finding["fingerprint"] = history.fingerprint(finding["path"], finding["title"])
    in_scope = [f for f in all_findings if paths.allows(f["path"])]
    path_filtered_out = len(all_findings) - len(in_scope)
    findings = sorted((f for f in in_scope if f["priority"] <= max_priority), key=lambda f: f["priority"])
    filtered_out = len(in_scope) - len(findings)
    print(f"Parsed {len(all_findings)} finding(s); {len(findings)} at P{max_priority} or above.")
    notes = []
    if path_filtered_out:
        print(f"Dropped {path_filtered_out} finding(s) outside include-paths/exclude-paths.")
        notes.append(f"{plural(path_filtered_out, 'finding')} outside the reviewed paths not shown.")
    if filtered_out:
        notes.append(f"{plural(filtered_out, 'lower-priority finding')} below P{max_priority} not shown.")
    if env.get("SIZE_NOTE", "").strip():
        notes.append(env["SIZE_NOTE"].strip())
    ctx.note = " ".join(notes)

    # What the previous review reported, so this one can say what got fixed.
    plan = history.load_plan(env.get("STATE_FILE", ""))
    previous = plan.get("previous") or {}
    ctx.previous_sha = ctx.previous_sha or previous.get("sha", "")
    ctx.incremental = bool(plan.get("incremental"))
    ctx.resolved, ctx.carried, ctx.still_open = history.classify(
        previous.get("findings") or [], findings, plan.get("changed-files")
    )
    ctx.state = history.state_marker(ctx.head_sha, findings, ctx.carried, history.settings_digest(dict(env)))
    if previous:
        print(f"Since {history.short(ctx.previous_sha)}: {len(ctx.resolved)} resolved, "
              f"{len(ctx.still_open)} still open, {len(ctx.carried)} not re-checked.")

    write_outputs(findings, filtered_out, len(ctx.resolved), path_filtered_out)
    write_summary_file(summary)
    write_summary(summary, findings, ctx)

    if mode != "none":
        if env.get("HIDE_PREVIOUS", "true").strip().lower() == "true":
            known = plan.get("hide")  # read with the state above, so not listed again
            print(f"Hid {hide_previous(repo, pr, token, env.get('STATUS_COMMENT_ID', ''), known)} earlier Codex comment(s).")
        if mode == "comment" or not findings:
            github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body_markdown(summary, findings, ctx)})
            print(f"Posted comment with {plural(len(findings), 'finding')}.")
        else:
            post_review(repo, pr, token, summary, findings, ctx, native_suggestions)
        set_output("posted", "true")
        if ctx.resolved and env.get("RESOLVE_FIXED_THREADS", "true").strip().lower() == "true":
            fixed = {item["fp"] for item in ctx.resolved}
            threads = plan.get("threads")  # read with the state above, so not listed again
            print(f"Resolved {history.resolve_threads(repo, pr, token, fixed, github, GRAPHQL, threads)} fixed thread(s).")

    if fail_on is not None and findings and min(f["priority"] for f in findings) <= fail_on:
        print(f"::error::Codex found P{min(f['priority'] for f in findings)} issues (fail-on-priority is P{fail_on}).")
        return 1
    return 0


def post_review(repo: str, pr: str, token: str, summary: str, findings: list[dict], ctx: Context,
                native_suggestions: bool = True) -> None:
    """One review: verdict and full issues table in the body, inline comments on diff lines."""
    allowed = commentable_lines(repo, pr, token)
    inline = [f for f in findings if f["end"] in allowed.get(f["path"], set())]

    comments = []
    for finding in inline:
        comment = {"path": finding["path"], "line": finding["end"], "side": "RIGHT"}
        # A single-line finding always anchors exactly; a range only when its
        # first line is in the diff too, otherwise the comment covers just the last.
        exact_range = finding["start"] == finding["end"]
        if finding["start"] < finding["end"] and finding["start"] in allowed[finding["path"]]:
            comment.update(start_line=finding["start"], start_side="RIGHT")
            exact_range = True
        comment["body"] = inline_comment(finding, ctx, exact_range, native_suggestions)
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

#!/usr/bin/env python3
"""Re-review awareness: remember what was reported, notice what got fixed.

Every review this action posts carries a hidden state marker

    <!-- codex-pr-review-state {"v":1,"sha":"<head sha>","findings":[...],
                                "vd":"<verdict>","hs":<health score>} -->

with the reviewed commit and one fingerprint per finding, and every inline
comment carries its finding's fingerprint. On the next run the most recent
marker on the pull request is read back, so the new review can list what is no
longer reported ("Resolved since last review"), tag findings that came back as
still open, resolve the inline threads of fixed findings, and, with
`incremental`, review only the commits pushed since that commit.

A fingerprint is the path plus the meaningful words of the title, so it survives
line shifts and word order; a token overlap check catches larger rewordings.

    history.py plan   read the latest state, decide the base ref for this run

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import filters  # noqa: E402

STATE_PREFIX = "<!-- codex-pr-review-state "
STATE_RE = re.compile(re.escape(STATE_PREFIX) + r"(\{.*?\})\s*-->", re.DOTALL)
FINDING_PREFIX = "<!-- codex-pr-review-finding "
FINDING_RE = re.compile(re.escape(FINDING_PREFIX) + r"([0-9a-f]+)\s*-->")
# Words too common to tell two findings apart.
STOPWORDS = frozenset(
    "a an and are as at be by can for from has have in into is it its may not of on or "
    "should that the their there these this to when where which will with without".split()
)
# Keep the marker small: one line per finding, titles clipped.
TITLE_LIMIT = 120
# Below this many entries a section is shown open rather than collapsed.
COLLAPSE_FROM = 4
# Token overlap (Jaccard) at which two titles on the same path are the same finding.
SIMILAR_ENOUGH = 0.5


# Fingerprints ----------------------------------------------------------------


def title_tokens(title: str) -> frozenset:
    """Meaningful lowercase words of a title, without numbers (lines move)."""
    words = re.findall(r"[a-z0-9_]+", (title or "").lower())
    return frozenset(w for w in words if w not in STOPWORDS and not w.isdigit())


def fingerprint(path: str, title: str) -> str:
    """Stable id for a finding: path plus the title's meaningful words, sorted."""
    words = " ".join(sorted(title_tokens(title)))
    digest = hashlib.sha1(f"{(path or '').strip().lower()}\n{words}".encode())
    return digest.hexdigest()[:12]


def similarity(left: frozenset, right: frozenset) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# State marker ----------------------------------------------------------------


def entry(finding: dict) -> dict:
    """The part of a finding worth remembering for the next review."""
    title = re.sub(r"[<>]", "", finding.get("title") or "").strip()
    return {
        "fp": finding.get("fingerprint") or fingerprint(finding.get("path", ""), title),
        "pri": int(finding.get("priority", 3)),
        "path": finding.get("path", ""),
        "line": int(finding.get("start") or 0),
        "title": title[:TITLE_LIMIT],
    }


def state_marker(head_sha: str, findings: list, carried: list | None = None, config: str = "",
                 health=None) -> str:
    """The hidden state to append to a posted body."""
    entries = [entry(f) for f in findings] + list(carried or [])
    payload = {"v": 1, "sha": head_sha, "findings": entries}
    if config:
        # What the review was produced with, so the next run can tell whether
        # asking again could say anything new (see rereview.py).
        payload["cfg"] = config
    if health is not None:
        # The verdict and health score this review reached, so the next one can
        # show the trend and a skipped re-review can repeat the answer.
        payload["vd"] = health.verdict
        payload["hs"] = health.score
    return STATE_PREFIX + json.dumps(payload, separators=(",", ":")) + " -->"


# The settings that decide what a review says. Everything else (the heading, the
# re-run hint, the sandbox, how the findings are rendered) can change without
# making the same commit worth reviewing again.
SETTINGS = (
    ("provider", "PROVIDER", "lower"),
    ("model", "MODEL", ""),
    ("effort", "REASONING_EFFORT", "lower"),
    ("base", "BASE_REF", ""),
    ("incremental", "INCREMENTAL", "lower"),
    ("suggestions", "SUGGESTIONS", "lower"),
    ("max-priority", "MAX_PRIORITY", "upper"),
    ("post-mode", "POST_MODE", "lower"),
)


def settings_digest(env: dict) -> str:
    """Fingerprint of the settings a review was produced with.

    Pattern lists are sorted, so reordering `exclude-paths` is not a change, and
    the instructions go in as a digest of their text, whether they came from the
    input or from a file.
    """
    parts = []
    for name, variable, case in SETTINGS:
        value = (env.get(variable) or "").strip()
        value = value.lower() if case == "lower" else value.upper() if case == "upper" else value
        parts.append(f"{name}={value}")
    for name, variable in (("include", "INCLUDE_PATHS"), ("exclude", "EXCLUDE_PATHS")):
        parts.append(f"{name}=" + ",".join(sorted(filters.parse_patterns(env.get(variable, "")))))
    parts.append("instructions=" + hashlib.sha1(instructions(env).encode("utf-8")).hexdigest())
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def instructions(env: dict) -> str:
    """The review guidelines this run uses, inline ones and the file together."""
    import write_config  # noqa: E402  (only needed here, and it imports filters)

    try:
        return write_config.load_instructions(env)
    except SystemExit:
        # A configured file that is not there: a real difference from any run
        # that could read it, and write_config.py fails the run in a moment.
        return "\x00review-instructions-file-missing"


def finding_marker(fp: str) -> str:
    """The hidden id to append to an inline comment, so its thread is findable."""
    return f"{FINDING_PREFIX}{fp} -->"


def parse_state(body: str) -> dict | None:
    """The state in a comment body, or None when it has none (or a broken one)."""
    match = STATE_RE.search(body or "")
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return None
    return state if isinstance(state, dict) and isinstance(state.get("findings"), list) else None


def strip_state(text: str) -> str:
    """Drop the hidden markers, for places the state would only be noise."""
    return FINDING_RE.sub("", STATE_RE.sub("", text or ""))


# Reading the last review -----------------------------------------------------


def paginate(path: str, token: str, call) -> list:
    items, page = [], 1
    joiner = "&" if "?" in path else "?"
    while True:
        batch = call("GET", f"{path}{joiner}per_page=100&page={page}", token) or []
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def latest_state(repo: str, pr: str, token: str, call, items: list | None = None) -> dict:
    """State from the most recent earlier Codex review or comment on the PR.

    `items` are comment and review bodies already in hand (see pr_state.py), so
    the conversation is listed once per run instead of once per reader.
    """
    bodies = list(items) if items is not None else []
    if items is None:
        for path in (f"/repos/{repo}/issues/{pr}/comments", f"/repos/{repo}/pulls/{pr}/reviews"):
            try:
                bodies.extend(paginate(path, token, call))
            except (urllib.error.URLError, OSError) as error:
                print(f"::warning::Could not read earlier Codex reviews ({error}).")
    dated = []
    for item in bodies:
        state = parse_state(item.get("body") or "")
        if state:
            dated.append((item.get("submitted_at") or item.get("created_at") or "", state,
                          item.get("html_url") or ""))
    if not dated:
        return {}
    dated.sort(key=lambda found: found[0])
    state = dict(dated[-1][1])
    if dated[-1][2]:
        state["url"] = dated[-1][2]  # where to send a reader who asks again
    return state


# Classification --------------------------------------------------------------


def _match(previous: dict, findings: list) -> dict | None:
    """The finding reported again for a previous entry, exactly or reworded."""
    for finding in findings:
        if finding.get("fingerprint") == previous.get("fp"):
            return finding
    tokens = title_tokens(previous.get("title", ""))
    best, score = None, SIMILAR_ENOUGH
    for finding in findings:
        if finding.get("path") != previous.get("path"):
            continue
        overlap = similarity(tokens, title_tokens(finding.get("title", "")))
        if overlap >= score:
            best, score = finding, overlap
    return best


def classify(previous: list, findings: list, changed_files: list | None = None):
    """Split a previous review's findings into fixed, carried over and repeated.

    Returns (resolved, carried, still_open): previous entries no longer
    reported, previous entries an incremental pass did not re-check (their file
    is untouched by the new commits), and the fingerprints reported again.
    With changed_files None (a full review) nothing is carried over.
    """
    touched = None if changed_files is None else {p for p in changed_files}
    resolved, carried, still_open = [], [], set()
    for item in previous or []:
        match = _match(item, findings)
        if match:
            still_open.add(match["fingerprint"])
        elif touched is not None and item.get("path") not in touched:
            carried.append(item)
        else:
            resolved.append(item)
    return resolved, carried, still_open


# Rendering -------------------------------------------------------------------


def entry_link(item: dict, ctx) -> str:
    """`path:line` of a previous finding, linked at the commit it was found on."""
    path, line = item.get("path", ""), item.get("line") or 0
    label = f"`{path}:{line}`" if line else f"`{path}`"
    sha = getattr(ctx, "previous_sha", "") or ctx.head_sha
    if not (path and sha and ctx.repo):
        return label
    anchor = f"#L{line}" if line else ""
    return f"[{label}]({ctx.server}/{ctx.repo}/blob/{sha}/{path}{anchor})"


def _section(heading: str, items: list) -> str:
    """A list of previous findings, collapsed once it gets long."""
    body = "\n".join(items)
    if len(items) < COLLAPSE_FROM:
        return f"**{heading}**\n\n{body}"
    return f"<details>\n<summary><b>{html.escape(heading)}</b></summary>\n\n{body}\n\n</details>"


def resolved_section(resolved: list, carried: list, ctx) -> str:
    """"Resolved since last review", plus anything an incremental pass skipped."""
    blocks = []
    image = ctx.icons.img("resolved") if getattr(ctx, "icons", None) else ""
    mark = f"{image} " if image else ""
    if resolved:
        blocks.append(_section(
            f"Resolved since last review ({len(resolved)})",
            [f"- {mark}~~{item.get('title', '')}~~ \u00b7 {entry_link(item, ctx)}" for item in resolved],
        ))
    if carried:
        # Incremental passes skip untouched files; full passes read them, but a
        # finding in an unchanged file can't have been fixed.
        heading = "Still open, not re-checked" if getattr(ctx, "incremental", False) else "Still open, file unchanged"
        blocks.append(_section(
            f"{heading} ({len(carried)})",
            [f"- P{item.get('pri', 3)} {item.get('title', '')} \u00b7 {entry_link(item, ctx)}" for item in carried],
        ))
    return "\n\n".join(blocks)


# Resolving inline threads ----------------------------------------------------


THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 1) { nodes { body } } }
      }
    }
  }
}
"""
RESOLVE_MUTATION = (
    "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { clientMutationId } }"
)


def review_threads(repo: str, pr: str, token: str, call, graphql: str) -> list:
    """Unresolved threads on the PR as (thread id, fingerprint) pairs."""
    owner, _, name = repo.partition("/")
    threads, cursor = [], None
    while True:
        variables = {"owner": owner, "name": name, "number": int(pr), "cursor": cursor}
        data = call("POST", "", token, {"query": THREADS_QUERY, "variables": variables}, url=graphql)
        page = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        page = page.get("reviewThreads") or {}
        for node in page.get("nodes") or []:
            comments = (node.get("comments") or {}).get("nodes") or []
            match = FINDING_RE.search(comments[0].get("body") or "") if comments else None
            if match and not node.get("isResolved"):
                threads.append((node["id"], match.group(1)))
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return threads
        cursor = info.get("endCursor")


def resolve_threads(repo: str, pr: str, token: str, fingerprints: set, call, graphql: str,
                    threads: list | None = None) -> int:
    """Resolve the threads of findings that are fixed. Best effort, never fatal.

    `threads` are the (thread id, fingerprint) pairs already read for this run
    (see pr_state.py); without them they are listed here.
    """
    if not fingerprints:
        return 0
    if threads is None:
        try:
            threads = review_threads(repo, pr, token, call, graphql)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as error:
            print(f"::warning::Could not list review threads ({error}).")
            return 0
    resolved = 0
    for thread_id, fp in threads:
        if fp not in fingerprints:
            continue
        try:
            call("POST", "", token, {"query": RESOLVE_MUTATION, "variables": {"id": thread_id}}, url=graphql)
            resolved += 1
        except (urllib.error.URLError, OSError) as error:
            print(f"::warning::Could not resolve a fixed finding's thread ({error}).")
    return resolved


# Planning the run ------------------------------------------------------------


def git(*args: str) -> tuple[int, str]:
    process = subprocess.run(["git", *args], capture_output=True, text=True)
    return process.returncode, process.stdout.strip()


def short(sha: str) -> str:
    return (sha or "")[:7]


def incremental_plan(previous_sha: str, head_sha: str) -> dict:
    """Decide whether the new commits alone can be reviewed, and say why not."""
    if not previous_sha:
        return {}
    if previous_sha == head_sha:
        return {"note": "Full review (no new commits since the last review)", "changed-files": []}
    known = git("cat-file", "-e", f"{previous_sha}^{{commit}}")[0] == 0
    if not known or git("merge-base", "--is-ancestor", previous_sha, head_sha)[0] != 0:
        return {
            "note": f"Full review (last reviewed `{short(previous_sha)}` is no longer in this branch's history)",
            "changed-files": None,
        }
    changed = git("diff", "--name-only", previous_sha, head_sha)[1]
    return {
        "incremental": True,
        "base": previous_sha,
        "changed-files": [line for line in changed.splitlines() if line],
        "note": f"Incremental: `{short(previous_sha)}`..`{short(head_sha)}`",
    }


def changed_since(previous_sha: str, head_sha: str) -> list | None:
    """Files changed since the last reviewed commit, which decides what can count
    as fixed: a finding whose file did not change was not fixed, whatever Codex
    reports this time (it is not deterministic, and rewords or drops findings on
    an identical commit). None when that commit can't be compared (not in this
    history, e.g. after a force-push), so every unreported finding counts as fixed.
    """
    if not previous_sha:
        return None
    if previous_sha == head_sha:
        return []
    known = git("cat-file", "-e", f"{previous_sha}^{{commit}}")[0] == 0
    if not known or git("merge-base", "--is-ancestor", previous_sha, head_sha)[0] != 0:
        return None
    return [line for line in git("diff", "--name-only", previous_sha, head_sha)[1].splitlines() if line]


def load_plan(path: str) -> dict:
    """The plan written by `history.py plan`, or an empty one when there is none."""
    file = pathlib.Path(path) if path else None
    if not file or not file.is_file():
        return {}
    try:
        plan = json.loads(file.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return plan if isinstance(plan, dict) else {}


def plan(env: dict, call, bundle: dict | None = None) -> dict:
    """Read the last review's state and work out what this run should review."""
    repo, pr, token = env["GITHUB_REPOSITORY"], env["PR_NUMBER"], env.get("GH_TOKEN", "")
    head_sha, base_ref = env.get("HEAD_SHA", ""), env.get("BASE_REF", "")
    previous = latest_state(repo, pr, token, call, (bundle or {}).get("items"))
    result = {"previous": previous, "incremental": False, "base": base_ref, "note": ""}
    count = len(previous.get("findings") or []) if previous else 0
    print(f"Previous Codex state: {'none' if not previous else short(previous.get('sha', '')) + f', {count} finding(s)'}")
    if env.get("INCREMENTAL", "").strip().lower() == "true":
        result.update(incremental_plan(previous.get("sha", "") if previous else "", head_sha))
        if result["note"]:
            print(result["note"])
    if previous and "changed-files" not in result:
        result["changed-files"] = changed_since(previous.get("sha", ""), head_sha)
    return result


def main(action: str) -> int:
    if action != "plan":
        print(f"::error::Unknown history action '{action}'.")
        return 1
    from publish_review import GRAPHQL, github  # noqa: E402  (imported here: publish_review imports this module)
    import pr_state  # noqa: E402  (imports publish_review, so not at module level)

    env = os.environ
    repo, pr = env["GITHUB_REPOSITORY"], env["PR_NUMBER"]
    # One read of the conversation for the whole run: the state marker below, the
    # comments publish_review.py collapses, and the threads it resolves.
    bundle = pr_state.fetch(repo, pr, env.get("GH_TOKEN", ""), github, GRAPHQL)
    result = plan(dict(env), github, bundle)
    if bundle:
        result["hide"] = bundle["hide"]
        result["threads"] = bundle["threads"]
        result["signals"] = bundle.get("signals")
    state_file = env.get("STATE_FILE", "")
    if state_file:
        pathlib.Path(state_file).write_text(json.dumps(result), encoding="utf-8")
    outputs = {
        "review-base": result["base"],
        "incremental": "true" if result["incremental"] else "false",
        "previous-sha": (result.get("previous") or {}).get("sha", ""),
        "note": result["note"],
    }
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in outputs.items():
                out.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
    except (urllib.error.URLError, KeyError, OSError) as error:
        # Re-review awareness is a nicety: a full review is always a safe fallback.
        print(f"::warning::Could not read earlier Codex reviews: {error}")
        sys.exit(0)

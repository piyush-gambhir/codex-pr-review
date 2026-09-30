#!/usr/bin/env python3
"""One request for everything a run needs from the pull request conversation.

A review used to list the pull request four times over: the issue comments and
the reviews to find the last state marker, then the issue comments and the
review comments again to collapse earlier Codex output, and the review threads
to resolve fixed findings. One GraphQL query returns all of it, so the run makes
one call where it made four or more, and the node ids `minimizeComment` needs
come back with it instead of being looked up again.

The bundle is read once, before the run posts anything, and handed to the later
steps through the plan file, so nothing is fetched twice:

    {"items": [...],      comment and review bodies, for the state marker
     "hide": [...],       ids of earlier Codex comments, for hide-previous
     "threads": [...],    unresolved (thread id, fingerprint) pairs
     "signals": {...}}    draft, mergeable and the check rollup, for the health score

REST stays the fallback: when the query fails or cannot cover the whole
conversation (an old GitHub Enterprise Server, a token GraphQL refuses, more
than a page of threads) the affected entry is None and its caller goes back to
the paginated REST listing it used before.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import history  # noqa: E402
from publish_review import MARKER, MARKER_PREFIX  # noqa: E402

# `last: 100` on the two conversation lists: the newest state marker is the one
# that counts, and GitHub caps a page at 100 nodes.
PAGE = 100
PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      isDraft
      mergeable
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      comments(last: %(page)d) {
        totalCount
        nodes { id databaseId body createdAt url }
      }
      reviews(last: %(page)d) {
        totalCount
        nodes { id databaseId body submittedAt url }
      }
      reviewThreads(first: %(page)d) {
        pageInfo { hasNextPage }
        nodes {
          isResolved
          id
          comments(first: 1) { nodes { id databaseId body } }
        }
      }
    }
  }
}
""" % {"page": PAGE}


def _pull_request(data: dict) -> dict:
    """The pullRequest node of a GraphQL answer, or {} when it isn't there."""
    repository = ((data or {}).get("data") or {}).get("repository") or {}
    return repository.get("pullRequest") or {}


def _nodes(connection: dict) -> list:
    return [node for node in ((connection or {}).get("nodes") or []) if isinstance(node, dict)]


def _is_ours(body: str) -> bool:
    """Did this action write the comment? Every one starts with its marker."""
    body = body or ""
    return body.startswith(MARKER_PREFIX) or MARKER in body


def items(pull: dict) -> list:
    """Comment and review bodies in the shape history.latest_state reads."""
    found = []
    for node in _nodes(pull.get("comments")):
        found.append({"body": node.get("body") or "", "created_at": node.get("createdAt") or "",
                      "html_url": node.get("url") or ""})
    for node in _nodes(pull.get("reviews")):
        found.append({"body": node.get("body") or "", "submitted_at": node.get("submittedAt") or "",
                      "html_url": node.get("url") or ""})
    return found


def hideable(pull: dict) -> list:
    """Earlier Codex comments to collapse, as {node_id, id} pairs.

    None when the conversation is longer than one page, so hide-previous still
    reaches every earlier comment through REST rather than only the newest 100.
    """
    comments, threads = pull.get("comments") or {}, pull.get("reviewThreads") or {}
    if (comments.get("totalCount") or 0) > PAGE or (threads.get("pageInfo") or {}).get("hasNextPage"):
        return None
    found = []
    for node in _nodes(comments):
        if _is_ours(node.get("body")):
            found.append({"node_id": node.get("id"), "id": node.get("databaseId")})
    # Our inline comments always start their thread, so the first comment of
    # each thread is the one that may carry the marker.
    for thread in _nodes(threads):
        for node in _nodes(thread.get("comments")):
            if _is_ours(node.get("body")):
                found.append({"node_id": node.get("id"), "id": node.get("databaseId")})
    return [entry for entry in found if entry["node_id"]]


def threads(pull: dict) -> list:
    """Unresolved (thread id, finding fingerprint) pairs, or None past one page."""
    connection = pull.get("reviewThreads") or {}
    if (connection.get("pageInfo") or {}).get("hasNextPage"):
        return None
    found = []
    for thread in _nodes(connection):
        if thread.get("isResolved"):
            continue
        for node in _nodes(thread.get("comments"))[:1]:
            match = history.FINDING_RE.search(node.get("body") or "")
            if match and thread.get("id"):
                found.append([thread["id"], match.group(1)])
    return found


def signals(pull: dict) -> dict:
    """What the pull request itself says about merging it, for the health score.

    `mergeable` is computed lazily by GitHub, so it is often `UNKNOWN` on a
    fresh query, and the rollup covers the head commit's checks - this workflow
    included, which is why only a rollup that actually failed ever counts.
    """
    commits = _nodes(pull.get("commits"))
    rollup = ((commits[0].get("commit") if commits else None) or {}).get("statusCheckRollup") or {}
    return {
        "draft": bool(pull.get("isDraft")),
        "mergeable": pull.get("mergeable") or "",
        "checks": rollup.get("state") or "",
    }


def fetch(repo: str, pr: str, token: str, call, graphql: str) -> dict:
    """The whole bundle in one GraphQL call, or None when it cannot be had."""
    owner, _, name = (repo or "").partition("/")
    variables = {"owner": owner, "name": name, "number": int(pr)}
    try:
        data = call("POST", "", token, {"query": PR_QUERY, "variables": variables}, url=graphql)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"::warning::Could not read the pull request in one request ({error}); using REST.")
        return None
    if (data or {}).get("errors"):
        print(f"::warning::GraphQL refused the pull request query ({data['errors'][0].get('message', '')[:200]}); using REST.")
        return None
    pull = _pull_request(data)
    if not pull:
        print("::warning::The pull request query returned nothing; using REST.")
        return None
    return {"items": items(pull), "hide": hideable(pull), "threads": threads(pull),
            "signals": signals(pull)}

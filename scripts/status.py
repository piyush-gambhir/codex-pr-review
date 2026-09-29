#!/usr/bin/env python3
"""Progress feedback on the pull request while a review runs.

    status.py start  posts an "in progress" comment (id written to GITHUB_OUTPUT)
    status.py done   removes that comment and reacts to the request with a rocket
    status.py skip   posts a note that the pull request is too large to review
    status.py fail   turns that comment (or a new one) into a failure note with
                     the reason and a link to the run, and reacts with "confused"

Every call is best effort: feedback must never fail the review itself.
Standard library only.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from publish_review import MARKER_PREFIX, Context, github  # noqa: E402

STATUS_MARKER = f"{MARKER_PREFIX}-status -->"


def react(repo: str, token: str, comment_id: str, content: str) -> None:
    if comment_id:
        github("POST", f"/repos/{repo}/issues/comments/{comment_id}/reactions", token, {"content": content})


def failure_reason(events_file: str) -> str:
    """The last meaningful error Codex reported, if any."""
    path = pathlib.Path(events_file) if events_file else None
    if not path or not path.is_file():
        return ""
    reason = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") in ("error", "turn.failed"):
            message = event.get("message") or (event.get("error") or {}).get("message") or ""
            if message and not message.startswith("Reconnecting"):
                reason = message
    return reason[:500]


def main(action: str) -> int:
    env = os.environ
    repo, pr, token = env["GITHUB_REPOSITORY"], env["PR_NUMBER"], env.get("GH_TOKEN", "")
    ctx = Context(dict(env))
    status_id = env.get("STATUS_COMMENT_ID", "")
    trigger_id = env.get("TRIGGER_COMMENT_ID", "")

    if action == "start":
        body = (
            f"{STATUS_MARKER}\n\U0001f50d **{ctx.title} in progress**\n\n"
            f"<sub>{ctx.meta('Reviewing')}. Usually takes a minute or two; this note is replaced when the review is posted.</sub>"
        )
        comment = github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body})
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"status-comment-id={comment['id']}\n")
    elif action == "done":
        if status_id:
            github("DELETE", f"/repos/{repo}/issues/comments/{status_id}", token)
        react(repo, token, trigger_id, "rocket")
    elif action == "skip":
        # The size guard decided not to review at all; there is no progress note
        # yet, so this is the only thing the pull request gets.
        body = (
            f"{STATUS_MARKER}\n\u23ed\ufe0f **{ctx.title} skipped**\n\n"
            f"PR too large to review ({env.get('CHANGED_LINES', '?')} changed lines, "
            f"limit {env.get('MAX_CHANGED_LINES', '?')}).\n\n"
            f"<sub>{ctx.meta('Skipped')}</sub>" + (f"\n<sub>{ctx.rerun_hint}</sub>" if ctx.rerun_hint else "")
        )
        github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body})
    elif action == "fail":
        reason = failure_reason(env.get("EVENTS_FILE", ""))
        detail = f"\n\n```\n{reason}\n```" if reason else ""
        body = (
            f"{STATUS_MARKER}\n❌ **{ctx.title} failed**{detail}\n\n"
            f"<sub>{ctx.meta('Reviewing')}</sub>" + (f"\n<sub>{ctx.rerun_hint}</sub>" if ctx.rerun_hint else "")
        )
        if status_id:
            github("PATCH", f"/repos/{repo}/issues/comments/{status_id}", token, {"body": body})
        else:
            github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body})
        react(repo, token, trigger_id, "confused")
    else:
        print(f"::error::Unknown status action '{action}'.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
    except (urllib.error.URLError, KeyError, OSError) as error:
        print(f"::warning::Could not update review status on the PR: {error}")
        sys.exit(0)

#!/usr/bin/env python3
"""Don't pay for a review that would say the same thing again.

The state marker in the last Codex review on a pull request names the commit it
reviewed and a digest of the settings that produced it (history.settings_digest).
When both match this run, calling Codex again costs a model call and posts the
findings that are already on the pull request, so the run posts a short note
pointing at that review instead and everything after this step is skipped.

    rereview.py check   decide, post the note, write the `skipped` output

Anything that could change the answer stops the skip: a new commit, a different
provider, model, effort, base ref, guidelines, path filters, priority cut-off or
post mode, a marker written before settings digests existed, or `force`.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import os
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import history  # noqa: E402
import status  # noqa: E402
from publish_review import Context, github, set_output  # noqa: E402

# Why the review ran after all. Only "already-reviewed" skips.
REASONS = {
    "forced": "force was asked for",
    "disabled": "skip-unchanged is off",
    "no-previous-review": "no earlier Codex review to reuse",
    "new-commit": "the head commit is not the one that was reviewed",
    "no-settings-digest": "the earlier review predates settings digests",
    "settings-changed": "the review settings changed",
    "already-reviewed": "this commit already has a Codex review with these settings",
}


def flag(value: str, default: bool = False) -> bool:
    value = (value or "").strip().lower()
    return default if value == "" else value == "true"


def decision(previous: dict, head_sha: str, digest: str, enabled: bool = True,
             force: bool = False) -> tuple:
    """(skip, reason): can this run reuse the review already on the pull request?"""
    if force:
        return False, "forced"
    if not enabled:
        return False, "disabled"
    if not previous:
        return False, "no-previous-review"
    if not head_sha or (previous.get("sha") or "") != head_sha:
        return False, "new-commit"
    if not previous.get("cfg"):
        return False, "no-settings-digest"
    if previous["cfg"] != digest:
        return False, "settings-changed"
    return True, "already-reviewed"


def note(ctx: Context, url: str) -> str:
    """The note that goes up instead of a review."""
    commit = ctx.commit_link() or "this commit"
    already = f"[a Codex review]({url})" if url else "a Codex review"
    lines = [
        f"{status.STATUS_MARKER}",
        f"\u23ed\ufe0f **{ctx.title} skipped**",
        "",
        f"{commit} already has {already} with these settings, so it was not reviewed again.",
        "",
        f"<sub>{ctx.meta('Skipped')}</sub>",
        "<sub>Add `force` to the request to review it again.</sub>",
    ]
    if ctx.rerun_hint:
        lines.append(f"<sub>{ctx.rerun_hint}</sub>")
    return "\n".join(lines)


def main(action: str) -> int:
    if action != "check":
        print(f"::error::Unknown rereview action '{action}'.")
        return 1
    env = os.environ
    repo, pr, token = env["GITHUB_REPOSITORY"], env["PR_NUMBER"], env.get("GH_TOKEN", "")
    plan = history.load_plan(env.get("STATE_FILE", ""))
    previous = plan.get("previous") or {}
    digest = history.settings_digest(dict(env))
    skip, reason = decision(
        previous,
        env.get("HEAD_SHA", "").strip(),
        digest,
        enabled=flag(env.get("SKIP_UNCHANGED"), True),
        force=flag(env.get("FORCE"), False),
    )
    url = previous.get("url", "")
    print(f"Settings digest {digest}; {REASONS.get(reason, reason)}.")
    set_output("skipped", "true" if skip else "false")
    set_output("skip-reason", reason if skip else "")
    set_output("review-url", url if skip else "")
    if not skip:
        return 0

    print(f"::notice::Skipping the review: {REASONS['already-reviewed']}.")
    if (env.get("POST_MODE") or "review").strip().lower() == "none":
        return 0  # nothing is posted in this mode, so there is no note either
    ctx = Context(dict(env))
    github("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": note(ctx, url)})
    # The request was answered, so it gets the same reaction a posted review gets.
    status.react(repo, token, env.get("TRIGGER_COMMENT_ID", ""), "rocket")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
    except (urllib.error.URLError, KeyError, OSError) as error:
        # Reviewing again is always safe, so a failure here never stops the run.
        print(f"::warning::Could not check for an unchanged re-review: {error}")
        set_output("skipped", "false")
        sys.exit(0)

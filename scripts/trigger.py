#!/usr/bin/env python3
"""Decide whether a Codex review should run, and with what settings.

Reads the triggering event and answers three questions: is this a review
request, is it allowed, and what should be reviewed. Three events trigger:

    issue_comment      "@gpt review [openai|bedrock] [low|medium|high|xhigh]
                       [full] [force]" at the start of a comment on an open
                       pull request
    pull_request       a configured label added to the pull request
    workflow_dispatch   a pull request number typed in the Actions tab

The answer is written to GITHUB_OUTPUT as run, pr-number, head-sha, base-ref,
provider, effort, full, force, comment-id and reason. A declined request is not
an error: run=false with a reason, so a stray comment leaves the workflow green.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from publish_review import github  # noqa: E402

EFFORTS = ("low", "medium", "high", "xhigh")
PROVIDERS = ("openai", "bedrock")
# Asks for a review even when the commit already has one with these settings.
FORCE_WORDS = ("force",)
# Asks for a full review: every changed file, shard by shard (see full_review.py).
FULL_WORDS = ("full",)
# getCollaboratorPermissionLevel reports maintain and triage as write and read.
WRITE_PERMISSIONS = ("admin", "maintain", "write")


class Skip(Exception):
    """Declined request: `reason` goes to the output, `message` to the log."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message


class Request(NamedTuple):
    """A review request, before it is checked against the pull request."""

    pr_number: int
    actor: str
    provider: str
    effort: str
    comment_id: str = ""
    from_label: bool = False
    force: bool = False
    full: bool = False


class Settings:
    """The action's inputs, plus what the event and the runner provide."""

    def __init__(self, env: dict[str, str]):
        self.repo = env.get("GITHUB_REPOSITORY", "")
        self.token = env.get("GH_TOKEN", "")
        self.actor = env.get("GITHUB_ACTOR", "")
        self.event_name = env.get("GITHUB_EVENT_NAME", "")
        self.event_path = env.get("GITHUB_EVENT_PATH", "")
        self.command = (env.get("COMMAND") or "@gpt review").strip()
        self.providers = split_list(env.get("ALLOWED_PROVIDERS", "")) or list(PROVIDERS)
        self.provider = (env.get("DEFAULT_PROVIDER") or "openai").strip().lower()
        self.effort = (env.get("DEFAULT_EFFORT") or "medium").strip().lower()
        self.label = (env.get("LABEL") or "").strip()
        self.remove_label = (env.get("REMOVE_LABEL") or "true").strip().lower() == "true"
        self.force = (env.get("FORCE") or "").strip().lower() == "true"
        self.full = (env.get("FULL") or "").strip().lower() == "true"
        self.dispatch_pr = (env.get("PR_NUMBER") or "").strip()
        self._base_branches = split_list(env.get("BASE_BRANCHES", ""))

    def base_branches(self, pull_request: dict | None = None) -> list[str]:
        """Allowed base branches; empty input means the repository default.

        The pull request payload already names it, so the repository is only
        fetched when it is not there.
        """
        if not self._base_branches:
            default = (((pull_request or {}).get("base") or {}).get("repo") or {}).get("default_branch")
            if not default:
                default = (github("GET", f"/repos/{self.repo}", self.token) or {})["default_branch"]
            self._base_branches = [default]
        return self._base_branches


def split_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def load_event(path: str) -> dict:
    if not path or not pathlib.Path(path).is_file():
        return {}
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def outputs(**values: object) -> None:
    """Write step outputs; underscores in names become dashes."""
    text = "".join(f"{name.replace('_', '-')}={value}\n" for name, value in values.items())
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(text)
    else:  # local runs still show what a workflow would receive
        sys.stdout.write(text)


# Parsing ---------------------------------------------------------------------


def parse_options(first_line: str, settings: Settings) -> tuple[str, str, bool, bool]:
    """Words after the command pick provider, effort, a full review and a forced
    re-review: `@gpt review bedrock high full force`."""
    provider, effort = settings.provider, settings.effort
    force, full = settings.force, settings.full
    # Known providers are recognised even when they aren't allowed, so asking
    # for a forbidden one is refused rather than silently reviewed on the other.
    for word in first_line.strip()[len(settings.command):].lower().split():
        if word in PROVIDERS or word in settings.providers:
            provider = word
        elif word in EFFORTS:
            effort = word
        elif word in FORCE_WORDS:
            force = True
        elif word in FULL_WORDS:
            full = True
        else:
            print(f"::notice::Ignoring unknown option '{word}'.")
    if provider not in settings.providers:
        raise Skip("invalid-provider", f"Provider '{provider}' is not one of {', '.join(settings.providers)}.")
    if effort not in EFFORTS:
        raise Skip("invalid-effort", f"Invalid reasoning effort '{effort}'; use {', '.join(EFFORTS)}.")
    return provider, effort, force, full


def build_request(settings: Settings, event: dict) -> Request:
    """Turn the event into a Request, or Skip when it isn't a review request."""
    if settings.event_name == "issue_comment":
        issue = event.get("issue") or {}
        comment = event.get("comment") or {}
        if not issue.get("pull_request"):
            raise Skip("not-a-pull-request")
        body = comment.get("body") or ""
        if not body.lstrip().startswith(settings.command):
            raise Skip("no-command")
        provider, effort, force, full = parse_options(body.lstrip().splitlines()[0], settings)
        return Request(int(issue["number"]), settings.actor, provider, effort,
                       str(comment.get("id") or ""), force=force, full=full)

    if settings.event_name in ("pull_request", "pull_request_target"):
        if not settings.label:
            raise Skip("no-label-configured", "No label input is set, so label triggers are off.")
        added = ((event.get("label") or {}).get("name") or "")
        if event.get("action") != "labeled" or added != settings.label:
            raise Skip("label-mismatch")
        number = int((event.get("pull_request") or {}).get("number") or event.get("number") or 0)
        # A label carries no options, so only the defaults are checked.
        provider, effort, force, full = parse_options(settings.command, settings)
        return Request(number, settings.actor, provider, effort, from_label=True,
                       force=force, full=full)

    if settings.event_name == "workflow_dispatch":
        if not settings.dispatch_pr.isdigit():
            raise Skip("no-pr-number", "workflow_dispatch needs a pull request number in the pr-number input.")
        provider, effort, force, full = parse_options(settings.command, settings)
        return Request(int(settings.dispatch_pr), settings.actor, provider, effort,
                       force=force, full=full)

    raise Skip("unsupported-event", f"Event '{settings.event_name}' does not trigger a review.")


# Checks ----------------------------------------------------------------------


def require_write_access(settings: Settings, actor: str) -> None:
    path = f"/repos/{settings.repo}/collaborators/{urllib.parse.quote(actor)}/permission"
    try:
        permission = (github("GET", path, settings.token) or {}).get("permission", "")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        permission = "none"  # not a collaborator at all
    if permission not in WRITE_PERMISSIONS:
        raise Skip("no-write-access", f"{actor} lacks write access; ignoring.")


def check_pull_request(settings: Settings, number: int) -> dict:
    pr = github("GET", f"/repos/{settings.repo}/pulls/{number}", settings.token)
    if pr.get("state") != "open":
        raise Skip("pull-request-not-open", f"PR #{number} is {pr.get('state')}.")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if head_repo != settings.repo:
        raise Skip("fork", "Fork PRs are not reviewed: the review job holds model credentials.")
    allowed = settings.base_branches(pr)
    base = (pr.get("base") or {}).get("ref", "")
    if base not in allowed:
        reply(settings, number, f"Codex review only runs on PRs targeting {as_code(allowed)} (this PR targets `{base}`).")
        raise Skip("base-branch-not-allowed", f"PR #{number} targets '{base}', not {', '.join(allowed)}.")
    return pr


def as_code(branches: list[str]) -> str:
    return " or ".join(f"`{branch}`" for branch in branches)


# Best-effort feedback on the pull request ------------------------------------


def reply(settings: Settings, number: int, body: str) -> None:
    try:
        github("POST", f"/repos/{settings.repo}/issues/{number}/comments", settings.token, {"body": body})
    except (urllib.error.URLError, OSError) as error:
        print(f"::warning::Could not post the reply on the pull request: {error}")


def react_eyes(settings: Settings, comment_id: str) -> None:
    try:
        path = f"/repos/{settings.repo}/issues/comments/{comment_id}/reactions"
        github("POST", path, settings.token, {"content": "eyes"})
    except (urllib.error.URLError, OSError) as error:
        print(f"::warning::Could not add the eyes reaction: {error}")


def remove_label(settings: Settings, number: int) -> None:
    """Take the trigger label off again so it can be re-added to re-run."""
    path = f"/repos/{settings.repo}/issues/{number}/labels/{urllib.parse.quote(settings.label)}"
    try:
        github("DELETE", path, settings.token)
    except (urllib.error.URLError, OSError) as error:
        print(f"::warning::Could not remove the '{settings.label}' label: {error}")


# -----------------------------------------------------------------------------


def main() -> int:
    settings = Settings(dict(os.environ))
    event = load_event(settings.event_path)
    labelled: int = 0  # pull request whose trigger label should come off again
    try:
        request = build_request(settings, event)
        if request.from_label and settings.remove_label:
            labelled = request.pr_number
        require_write_access(settings, request.actor)
        pr = check_pull_request(settings, request.pr_number)
        if request.comment_id:
            react_eyes(settings, request.comment_id)
        extras = ("" if not request.full else ", full") + (" forced" if request.force else "")
        print(f"Reviewing PR #{pr['number']} ({request.provider}, {request.effort} effort{extras}).")
        outputs(
            run="true",
            pr_number=pr["number"],
            head_sha=pr["head"]["sha"],
            base_ref="origin/" + pr["base"]["ref"],
            provider=request.provider,
            effort=request.effort,
            full="true" if request.full else "false",
            force="true" if request.force else "false",
            comment_id=request.comment_id,
            reason="",
        )
    except Skip as skip:
        if skip.message:
            print(f"::notice::{skip.message}")
        print(f"No review: {skip.reason}.")
        outputs(run="false", pr_number="", head_sha="", base_ref="", provider="", effort="",
                full="false", force="false", comment_id="", reason=skip.reason)
    finally:
        if labelled:
            remove_label(settings, labelled)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, KeyError, OSError, ValueError) as error:
        print(f"::error::Could not decide whether to review: {error}")
        outputs(run="false", reason="error")
        sys.exit(1)

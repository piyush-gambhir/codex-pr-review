import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import trigger  # noqa: E402

ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_ACTOR": "dev", "GH_TOKEN": "t",
       "BASE_BRANCHES": "main", "LABEL": "codex-review"}
HEAD = "b" * 40


def pull_request(number=7, state="open", base="main", head_repo="o/r", default_branch="main"):
    return {"number": number, "state": state,
            "base": {"ref": base, "repo": {"default_branch": default_branch}},
            "head": {"sha": HEAD, "repo": {"full_name": head_repo}}}


def comment_event(body="@gpt review", comment_id=99, number=7, is_pr=True):
    issue = {"number": number}
    if is_pr:
        issue["pull_request"] = {"url": "https://api/pulls/7"}
    return {"issue": issue, "comment": {"id": comment_id, "body": body}}


def label_event(name="codex-review", action="labeled", number=7):
    return {"action": action, "label": {"name": name}, "pull_request": {"number": number}}


class TriggerTest(unittest.TestCase):
    def run_trigger(self, event_name, event=None, permission="write", pr=None, **env):
        """Run main() against a fake event and API; returns (outputs, calls)."""
        calls = []
        response = pull_request() if pr is None else pr

        def api(method, path, token, payload=None, url=None):
            calls.append((method, path, payload))
            if path.endswith("/permission"):
                if permission is None:
                    raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
                return {"permission": permission}
            if "/pulls/" in path:
                return response
            if path == "/repos/o/r":
                return {"default_branch": "main"}
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp, "out")
            out.touch()
            full = {**ENV, **env, "GITHUB_EVENT_NAME": event_name, "GITHUB_OUTPUT": str(out)}
            if event is not None:
                path = pathlib.Path(tmp, "event.json")
                path.write_text(json.dumps(event), encoding="utf-8")
                full["GITHUB_EVENT_PATH"] = str(path)
            with mock.patch.dict(os.environ, full, clear=True), \
                    mock.patch.object(trigger, "github", side_effect=api):
                self.assertEqual(trigger.main(), 0)
            written = dict(line.split("=", 1) for line in out.read_text().splitlines() if line)
        return written, calls

    def paths(self, calls, method=None):
        return [path for verb, path, _ in calls if method in (None, verb)]

    # Option parsing ----------------------------------------------------------

    def options(self, body, **env):
        out, _ = self.run_trigger("issue_comment", comment_event(body), **env)
        return out["provider"], out["effort"]

    def test_options_default_and_override(self):
        self.assertEqual(self.options("@gpt review"), ("openai", "medium"))
        self.assertEqual(self.options("@gpt review high"), ("openai", "high"))
        self.assertEqual(self.options("@gpt review bedrock"), ("bedrock", "medium"))
        self.assertEqual(self.options("@gpt review BEDROCK xhigh"), ("bedrock", "xhigh"))
        self.assertEqual(self.options("@gpt review xhigh bedrock\nplease"), ("bedrock", "xhigh"))
        self.assertEqual(
            self.options("@gpt review", DEFAULT_PROVIDER="bedrock", DEFAULT_EFFORT="low"),
            ("bedrock", "low"),
        )

    def test_options_ignores_unknown_words_but_rejects_bad_values(self):
        self.assertEqual(self.options("@gpt review please be thorough"), ("openai", "medium"))
        out, calls = self.run_trigger(
            "issue_comment", comment_event("@gpt review bedrock"), ALLOWED_PROVIDERS="openai")
        self.assertEqual((out["run"], out["reason"]), ("false", "invalid-provider"))
        self.assertEqual(calls, [])  # nothing is asked of GitHub
        out, _ = self.run_trigger("issue_comment", comment_event(), DEFAULT_EFFORT="turbo")
        self.assertEqual(out["reason"], "invalid-effort")

    def test_custom_command(self):
        out, _ = self.run_trigger(
            "issue_comment", comment_event("/review high"), COMMAND="/review")
        self.assertEqual((out["run"], out["effort"]), ("true", "high"))

    # issue_comment -----------------------------------------------------------

    def test_comment_runs_and_reacts(self):
        out, calls = self.run_trigger("issue_comment", comment_event())
        self.assertEqual(out["run"], "true")
        self.assertEqual(out["pr-number"], "7")
        self.assertEqual(out["head-sha"], HEAD)
        self.assertEqual(out["base-ref"], "origin/main")
        self.assertEqual(out["comment-id"], "99")
        self.assertEqual(out["reason"], "")
        self.assertIn(("POST", "/repos/o/r/issues/comments/99/reactions", {"content": "eyes"}), calls)

    def test_comment_without_the_command_is_ignored(self):
        out, calls = self.run_trigger("issue_comment", comment_event("looks good to me"))
        self.assertEqual((out["run"], out["reason"]), ("false", "no-command"))
        self.assertEqual(calls, [])

    def test_comment_on_a_plain_issue_is_ignored(self):
        out, calls = self.run_trigger("issue_comment", comment_event(is_pr=False))
        self.assertEqual(out["reason"], "not-a-pull-request")
        self.assertEqual(calls, [])

    # Access and pull request checks ------------------------------------------

    def test_read_access_is_refused(self):
        out, calls = self.run_trigger("issue_comment", comment_event(), permission="read")
        self.assertEqual((out["run"], out["reason"]), ("false", "no-write-access"))
        self.assertNotIn("/repos/o/r/pulls/7", self.paths(calls))

    def test_non_collaborator_is_refused(self):
        out, _ = self.run_trigger("issue_comment", comment_event(), permission=None)
        self.assertEqual(out["reason"], "no-write-access")

    def test_fork_is_skipped(self):
        out, calls = self.run_trigger(
            "issue_comment", comment_event(), pr=pull_request(head_repo="fork/r"))
        self.assertEqual((out["run"], out["reason"]), ("false", "fork"))
        self.assertEqual(self.paths(calls, "POST"), [])  # no reply, no reaction

    def test_closed_pull_request_is_skipped(self):
        out, _ = self.run_trigger("issue_comment", comment_event(), pr=pull_request(state="closed"))
        self.assertEqual(out["reason"], "pull-request-not-open")

    def test_wrong_base_branch_replies_on_the_pull_request(self):
        out, calls = self.run_trigger(
            "issue_comment", comment_event(), pr=pull_request(base="release/1.x"),
            BASE_BRANCHES="main, develop")
        self.assertEqual((out["run"], out["reason"]), ("false", "base-branch-not-allowed"))
        reply = [payload for verb, path, payload in calls if path == "/repos/o/r/issues/7/comments"]
        self.assertEqual(len(reply), 1)
        self.assertIn("`main` or `develop`", reply[0]["body"])
        self.assertIn("`release/1.x`", reply[0]["body"])

    def test_several_base_branches_are_allowed(self):
        out, _ = self.run_trigger(
            "issue_comment", comment_event(), pr=pull_request(base="develop"),
            BASE_BRANCHES="main,develop")
        self.assertEqual((out["run"], out["base-ref"]), ("true", "origin/develop"))

    def test_empty_base_branches_falls_back_to_the_default_branch(self):
        out, calls = self.run_trigger("issue_comment", comment_event(), BASE_BRANCHES="")
        self.assertEqual(out["run"], "true")
        # The pull request payload already names it, so the repository is not fetched.
        self.assertNotIn("/repos/o/r", self.paths(calls, "GET"))

    def test_a_payload_without_a_default_branch_still_asks(self):
        pr = pull_request()
        pr["base"].pop("repo")
        out, calls = self.run_trigger("issue_comment", comment_event(), pr=pr, BASE_BRANCHES="")
        self.assertEqual(out["run"], "true")
        self.assertIn("/repos/o/r", self.paths(calls, "GET"))

    # force -------------------------------------------------------------------

    def test_force_asks_for_a_fresh_review(self):
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review force"))
        self.assertEqual((out["run"], out["force"]), ("true", "true"))
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review bedrock high FORCE"))
        self.assertEqual((out["provider"], out["effort"], out["force"]), ("bedrock", "high", "true"))

    def test_without_the_word_nothing_is_forced(self):
        out, _ = self.run_trigger("issue_comment", comment_event())
        self.assertEqual(out["force"], "false")

    def test_the_input_forces_triggers_that_carry_no_words(self):
        out, _ = self.run_trigger("pull_request", label_event(), FORCE="true")
        self.assertEqual((out["run"], out["force"]), ("true", "true"))
        out, _ = self.run_trigger("workflow_dispatch", {}, PR_NUMBER="7", FORCE="true")
        self.assertEqual((out["run"], out["force"]), ("true", "true"))

    def test_a_declined_request_is_never_forced(self):
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review force"), permission="read")
        self.assertEqual((out["run"], out["force"]), ("false", "false"))

    # full --------------------------------------------------------------------

    def test_full_asks_for_a_full_review(self):
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review full"))
        self.assertEqual((out["run"], out["full"]), ("true", "true"))
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review bedrock high FULL force"))
        self.assertEqual((out["provider"], out["effort"], out["full"], out["force"]),
                         ("bedrock", "high", "true", "true"))

    def test_without_the_word_a_review_is_a_single_pass(self):
        out, _ = self.run_trigger("issue_comment", comment_event())
        self.assertEqual(out["full"], "false")
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review force"))
        self.assertEqual((out["full"], out["force"]), ("false", "true"))

    def test_the_input_asks_for_it_on_triggers_that_carry_no_words(self):
        out, _ = self.run_trigger("pull_request", label_event(), FULL="true")
        self.assertEqual((out["run"], out["full"]), ("true", "true"))
        out, _ = self.run_trigger("workflow_dispatch", {}, PR_NUMBER="7", FULL="true")
        self.assertEqual((out["run"], out["full"]), ("true", "true"))

    def test_a_declined_request_asks_for_nothing(self):
        out, _ = self.run_trigger("issue_comment", comment_event("@gpt review full"), permission="read")
        self.assertEqual((out["run"], out["full"]), ("false", "false"))

    # pull_request labelled ---------------------------------------------------

    def test_label_runs_and_removes_the_label(self):
        out, calls = self.run_trigger("pull_request", label_event())
        self.assertEqual((out["run"], out["pr-number"], out["comment-id"]), ("true", "7", ""))
        self.assertEqual(self.paths(calls, "DELETE"), ["/repos/o/r/issues/7/labels/codex-review"])

    def test_label_is_removed_even_when_the_request_is_refused(self):
        out, calls = self.run_trigger("pull_request", label_event(), permission="read")
        self.assertEqual(out["reason"], "no-write-access")
        self.assertEqual(self.paths(calls, "DELETE"), ["/repos/o/r/issues/7/labels/codex-review"])

    def test_label_can_be_kept(self):
        out, calls = self.run_trigger("pull_request", label_event(), REMOVE_LABEL="false")
        self.assertEqual(out["run"], "true")
        self.assertEqual(self.paths(calls, "DELETE"), [])

    def test_other_labels_and_other_actions_are_ignored(self):
        out, calls = self.run_trigger("pull_request", label_event(name="bug"))
        self.assertEqual((out["run"], out["reason"]), ("false", "label-mismatch"))
        self.assertEqual(calls, [])
        out, _ = self.run_trigger("pull_request", label_event(action="opened"))
        self.assertEqual(out["reason"], "label-mismatch")

    def test_label_uses_the_defaults_and_validates_them(self):
        out, _ = self.run_trigger("pull_request", label_event(),
                                  DEFAULT_PROVIDER="bedrock", DEFAULT_EFFORT="xhigh")
        self.assertEqual((out["provider"], out["effort"]), ("bedrock", "xhigh"))
        out, calls = self.run_trigger("pull_request", label_event(), DEFAULT_PROVIDER="claude")
        self.assertEqual((out["run"], out["reason"]), ("false", "invalid-provider"))
        self.assertEqual(calls, [])

    def test_label_triggers_can_be_turned_off(self):
        out, _ = self.run_trigger("pull_request", label_event(), LABEL="")
        self.assertEqual(out["reason"], "no-label-configured")

    # workflow_dispatch -------------------------------------------------------

    def test_dispatch_with_a_pull_request_number(self):
        out, calls = self.run_trigger("workflow_dispatch", {}, PR_NUMBER="7",
                                      DEFAULT_EFFORT="high")
        self.assertEqual((out["run"], out["pr-number"], out["effort"]), ("true", "7", "high"))
        self.assertEqual(out["comment-id"], "")
        self.assertEqual(self.paths(calls, "POST"), [])  # nothing to react to

    def test_dispatch_without_a_pull_request_number(self):
        out, calls = self.run_trigger("workflow_dispatch", {})
        self.assertEqual((out["run"], out["reason"]), ("false", "no-pr-number"))
        self.assertEqual(calls, [])

    def test_unsupported_event(self):
        out, calls = self.run_trigger("push", {})
        self.assertEqual(out["reason"], "unsupported-event")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

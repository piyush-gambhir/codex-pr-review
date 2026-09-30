import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import history  # noqa: E402
import pr_state  # noqa: E402
import publish_review as pr  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
WORKSPACE = "/home/runner/work/repo/repo"
SHA = "0123456789abcdef0123456789abcdef01234567"
OLD_SHA = "89abcdef0123456789abcdef0123456789abcdef"
GRAPHQL = "https://api.github.com/graphql"


def entry(title, path="src/pricing.ts", priority=1, line=15):
    return {"fp": history.fingerprint(path, title), "pri": priority, "path": path,
            "line": line, "title": title}


def state_comment(entries, sha=OLD_SHA):
    return pr.MARKER + "\n## Codex review\n\n" + history.state_marker(sha, [], entries)


class FakePullRequest:
    """One pull request, answering both the GraphQL query and the REST listings.

    Every call is recorded, so a test can count what a run costs. `graphql_pr`
    off makes the shared read fail the way an old GitHub Enterprise Server or a
    refused token would, which puts every caller back on REST.
    """

    def __init__(self, comments=(), reviews=(), review_comments=(), threads=(), files=(),
                 graphql_pr=True):
        self.comments, self.reviews = list(comments), list(reviews)
        self.review_comments, self.threads = list(review_comments), list(threads)
        self.files, self.graphql_pr = list(files), graphql_pr
        self.calls = []
        self.posted_body = ""

    # Recorded call log ------------------------------------------------------

    def reads(self):
        """The calls that list part of the conversation."""
        return [name for name in self.calls if name.startswith("read:")]

    def writes(self):
        return [name for name in self.calls if not name.startswith("read:")]

    # The API ---------------------------------------------------------------

    def __call__(self, method, path, token, payload=None, url=None):
        if url:
            return self._graphql(payload or {})
        if "page=2" in path:
            return []  # one page of everything
        if path.startswith("/repos/o/r/issues/1/comments") and method == "GET":
            self.calls.append("read:issue-comments")
            return [dict(c, node_id=f"IC_{i}", id=100 + i) for i, c in enumerate(self.comments)]
        if path.startswith("/repos/o/r/pulls/1/reviews") and method == "GET":
            self.calls.append("read:reviews")
            return list(self.reviews)
        if path.startswith("/repos/o/r/pulls/1/comments") and method == "GET":
            self.calls.append("read:review-comments")
            return [dict(c, node_id=f"RC_{i}", id=200 + i) for i, c in enumerate(self.review_comments)]
        if path.startswith("/repos/o/r/pulls/1/files"):
            self.calls.append("read:files")
            return list(self.files)
        if method == "POST" and path.endswith("/pulls/1/reviews"):
            self.posted_body = (payload or {}).get("body", "")
        self.calls.append(f"{method} {path.split('?')[0]}")
        return {"id": 1}

    def _graphql(self, payload):
        query = payload.get("query", "")
        if "minimizeComment" in query:
            self.calls.append("minimize")
            return {}
        if "resolveReviewThread" in query:
            self.calls.append("resolve")
            return {}
        if "totalCount" in query:  # the shared read
            if not self.graphql_pr:
                self.calls.append("read:pull-request (refused)")
                return {"errors": [{"message": "Field 'x' doesn't exist"}]}
            self.calls.append("read:pull-request")
            return {"data": {"repository": {"pullRequest": self._pull()}}}
        self.calls.append("read:threads")
        return {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"id": t[0], "isResolved": False,
                       "comments": {"nodes": [{"id": "x", "databaseId": 9, "body": t[1]}]}}
                      for t in self.threads],
        }}}}}

    def _pull(self):
        return {
            "comments": {
                "totalCount": len(self.comments),
                "nodes": [{"id": f"IC_{i}", "databaseId": 100 + i, "body": c.get("body", ""),
                           "createdAt": c.get("created_at", ""), "url": c.get("html_url", "")}
                          for i, c in enumerate(self.comments)],
            },
            "reviews": {
                "totalCount": len(self.reviews),
                "nodes": [{"id": f"PRR_{i}", "databaseId": 300 + i, "body": r.get("body", ""),
                           "submittedAt": r.get("submitted_at", ""), "url": r.get("html_url", "")}
                          for i, r in enumerate(self.reviews)],
            },
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"id": t[0], "isResolved": False,
                           "comments": {"nodes": [{"id": f"RC_{i}", "databaseId": 200 + i, "body": t[1]}]}}
                          for i, t in enumerate(self.threads)],
            },
        }


class FetchTest(unittest.TestCase):
    def bundle(self, **kwargs):
        api = FakePullRequest(**kwargs)
        return pr_state.fetch("o/r", "1", "t", api, GRAPHQL), api

    def test_one_call_returns_bodies_comments_and_threads(self):
        marker = history.finding_marker("abc123456789")
        bundle, api = self.bundle(
            comments=[{"body": state_comment([entry("Old finding")]), "created_at": "2026-01-01T00:00:00Z",
                       "html_url": "https://github.com/o/r/pull/1#issuecomment-1"},
                      {"body": "unrelated chatter", "created_at": "2026-01-02T00:00:00Z"}],
            reviews=[{"body": "someone else's review", "submitted_at": "2026-01-03T00:00:00Z"}],
            threads=[("PRRT_1", f"a finding\n{marker}")],
        )
        self.assertEqual(api.calls, ["read:pull-request"])
        self.assertEqual(len(bundle["items"]), 3)
        self.assertEqual([c["node_id"] for c in bundle["hide"]], ["IC_0"])
        self.assertEqual(bundle["threads"], [["PRRT_1", "abc123456789"]])

    def test_only_this_actions_comments_are_hideable(self):
        bundle, _ = self.bundle(comments=[{"body": "plain comment"}, {"body": pr.MARKER + "\nreview"}],
                                threads=[("PRRT_1", "someone else's inline comment")])
        self.assertEqual([c["node_id"] for c in bundle["hide"]], ["IC_1"])
        self.assertEqual(bundle["threads"], [])

    def test_a_status_note_is_hideable_too(self):
        bundle, _ = self.bundle(comments=[{"body": "<!-- codex-pr-review-status -->\nin progress"}])
        self.assertEqual([c["node_id"] for c in bundle["hide"]], ["IC_0"])

    def test_a_conversation_longer_than_a_page_falls_back_for_hiding(self):
        api = FakePullRequest(comments=[{"body": pr.MARKER}])
        api._pull = lambda: dict(FakePullRequest._pull(api), comments={
            "totalCount": 250, "nodes": [{"id": "IC_0", "databaseId": 100, "body": pr.MARKER}]})
        bundle = pr_state.fetch("o/r", "1", "t", api, GRAPHQL)
        self.assertIsNone(bundle["hide"])
        self.assertIsNotNone(bundle["items"])

    def test_more_threads_than_a_page_falls_back_for_both(self):
        api = FakePullRequest()
        api._pull = lambda: dict(FakePullRequest._pull(api), reviewThreads={
            "pageInfo": {"hasNextPage": True}, "nodes": []})
        bundle = pr_state.fetch("o/r", "1", "t", api, GRAPHQL)
        self.assertIsNone(bundle["threads"])
        self.assertIsNone(bundle["hide"])

    def test_graphql_failures_fall_back(self):
        self.assertIsNone(self.bundle(graphql_pr=False)[0])

        def broken(*args, **kwargs):
            raise urllib.error.URLError("no route")

        self.assertIsNone(pr_state.fetch("o/r", "1", "t", broken, GRAPHQL))
        self.assertIsNone(pr_state.fetch("o/r", "1", "t", lambda *a, **k: {"data": {}}, GRAPHQL))


def run_review(api, previous_entries, **env):
    """One whole run against a fake API: history plan, then publish."""
    with tempfile.TemporaryDirectory() as tmp:
        review = pathlib.Path(tmp, "review.md")
        review.write_text((FIXTURES / "review-two-findings.md").read_text(), encoding="utf-8")
        out, state = pathlib.Path(tmp, "out"), pathlib.Path(tmp, "state.json")
        out.touch()
        base = {
            "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1", "GH_TOKEN": "t", "HEAD_SHA": SHA,
            "BASE_REF": "origin/main", "STATE_FILE": str(state), "GITHUB_OUTPUT": str(out),
            "REVIEW_FILE": str(review), "REVIEW_WORKSPACE": WORKSPACE, "RUNNER_TEMP": tmp,
            "POST_MODE": "review", "HIDE_PREVIOUS": "true", "RESOLVE_FIXED_THREADS": "true",
            "MODEL": "gpt-6.1-sol", "REASONING_EFFORT": "medium", "PROVIDER": "openai",
        }
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=True), mock.patch.object(pr, "github", api):
            self_code = history.main("plan")
            plan = json.loads(state.read_text())
            code = pr.main()
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if line)
    return self_code, code, plan, outputs


class CallCountTest(unittest.TestCase):
    """How many times one run reads the pull request, with and without the shared read."""

    def setUp(self):
        gone = entry("Validate the coupon code", path="src/coupons.ts", line=8)
        self.previous = [gone]
        self.gone = gone
        self.comments = [{"body": state_comment([gone]), "created_at": "2026-01-01T00:00:00Z",
                          "html_url": "https://github.com/o/r/pull/1#issuecomment-1"}]
        # An inline comment this action posted: the finding's fingerprint, then
        # the marker that makes it collapsible.
        inline = f"old inline\n{history.finding_marker(gone['fp'])}\n{pr.MARKER}"
        self.review_comments = [{"body": inline}]
        self.threads = [("PRRT_1", inline)]
        self.files = [{"filename": "src/pricing.ts",
                       "patch": "@@ -10,3 +10,16 @@\n+a\n+b\n+c\n+d\n+e\n+f\n+g\n+h\n+i\n+j\n+k\n+l\n+m"}]

    def api(self, graphql_pr):
        return FakePullRequest(comments=self.comments, review_comments=self.review_comments,
                              threads=self.threads, files=self.files, graphql_pr=graphql_pr)

    def test_the_shared_read_replaces_four_listings_with_one(self):
        rest = self.api(graphql_pr=False)
        _, _, plan, _ = run_review(rest, self.previous)
        self.assertIsNone(plan.get("hide"))
        self.assertEqual(rest.reads(), [
            "read:pull-request (refused)",   # the shared read is tried first
            "read:issue-comments", "read:reviews",        # history, on REST
            "read:issue-comments", "read:review-comments",  # hide-previous, again
            "read:files",                                 # the diff lines
            "read:threads",                               # the fixed finding's thread
        ])

        shared = self.api(graphql_pr=True)
        _, _, plan, _ = run_review(shared, self.previous)
        self.assertEqual(plan["hide"], [{"node_id": "IC_0", "id": 100},
                                        {"node_id": "RC_0", "id": 200}])
        self.assertEqual(shared.reads(), ["read:pull-request", "read:files"])

        print(f"conversation reads per run: {len(rest.reads()) - 1} on REST, "
              f"{len(shared.reads())} with the shared read")
        # The listings the fallback needs, minus its refused first attempt.
        self.assertEqual(len(rest.reads()) - 1, 6)
        self.assertEqual(len(shared.reads()), 2)

    def test_the_same_review_is_posted_either_way(self):
        results = []
        for graphql_pr in (False, True):
            api = self.api(graphql_pr)
            _, code, _, outputs = run_review(api, self.previous)
            results.append((code, outputs["findings-count"], outputs["resolved-count"],
                            api.writes()))
        self.assertEqual(results[0], results[1])
        code, found, resolved, writes = results[0]
        self.assertEqual((code, found, resolved), (0, "2", "1"))
        # Two earlier comments collapsed, the review posted, one fixed thread resolved.
        self.assertEqual(sorted(writes), ["POST /repos/o/r/pulls/1/reviews", "minimize", "minimize", "resolve"])

    def test_the_state_marker_carries_the_settings_digest(self):
        api = self.api(graphql_pr=True)
        run_review(api, self.previous)
        self.assertEqual(len([c for c in api.calls if c.startswith("POST")]), 1)
        self.assertEqual(history.parse_state(api.posted_body)["cfg"],
                         history.settings_digest({"PROVIDER": "openai", "MODEL": "gpt-6.1-sol",
                                                  "REASONING_EFFORT": "medium", "BASE_REF": "origin/main",
                                                  "POST_MODE": "review"}))


if __name__ == "__main__":
    unittest.main()

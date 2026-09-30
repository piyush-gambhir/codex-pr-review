#!/usr/bin/env python3
"""Should this pull request be merged? A verdict, a health score, a confidence.

Codex's own `overall_correctness` and `overall_confidence_score` fields come back
empty (`""` and `0.0`) on the Codex versions this action pins, so nothing here
comes from the model: it is all computed from what the run can actually see.

    verdict     ready | nits | changes-requested | blocked, from the worst
                priority still open, capped by the confidence
    health      0 to 100: 100 minus a weight per finding (capped per priority, so
                a pile of nits can never outrank one real bug) minus the pull
                request's own signals (failing checks, conflicts, draft)
    confidence  high | medium | low, from the coverage report a full-coverage
                pass writes, or from the size guard when there is none

"Still open" means the findings this review reported plus the ones the previous
review reported that this pass did not re-check (an incremental pass, or a file
that did not change). A finding is open until something says it is fixed.

The rules are deliberately dull and deterministic: the same findings on the same
pull request always produce the same verdict, score and confidence, so the
number means something across reviews and the trend line can be trusted.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.parse

# Verdicts, worst last: the index is the severity, so gating is a comparison.
READY = "ready"
NITS = "nits"
CHANGES = "changes-requested"
BLOCKED = "blocked"
ORDER = (READY, NITS, CHANGES, BLOCKED)

HEADLINE = {
    READY: "Ready to merge",
    NITS: "Mergeable, with nits",
    CHANGES: "Changes requested",
    BLOCKED: "Do not merge",
}
# GitHub's own alert blocks carry the verdict: it draws the octicon, the colour
# and the border, so the headline looks like the rest of the pull request page.
ALERT = {READY: "TIP", NITS: "NOTE", CHANGES: "WARNING", BLOCKED: "CAUTION"}

# A clean or nits-only result the run cannot stand behind is not "ready": it is
# reported as changes-requested, with a headline that says what is actually
# wrong (the review, not the code) and a NOTE rather than a WARNING, because
# nothing alarming was found.
INCOMPLETE_HEADLINE = "Needs a full review"
INCOMPLETE_ALERT = "NOTE"
INCOMPLETE_NOTE = (
    "The review did not cover the whole pull request, so what it found is not enough "
    "to call this mergeable."
)

# Points off per finding, and the most any one priority may take off in total.
# One P1 costs 20; every nit in the world costs at most 20 as well (15 + 5), so
# a pile of nits ties with one real bug and never beats it.
WEIGHT = {0: 40, 1: 20, 2: 5, 3: 1}
CAP = {0: 80, 1: 60, 2: 15, 3: 5}
# What the pull request itself says, read with the run's one GraphQL call.
SIGNAL_WEIGHT = {"checks": 10, "conflicts": 10, "draft": 5}
SIGNAL_LABEL = {
    "checks": "Failing checks",
    "conflicts": "Merge conflicts",
    "draft": "Draft pull request",
}

# Confidence thresholds, all documented in the README.
# Share of the changed files a partial pass has to have inspected to be `medium`.
COMPLETE_ENOUGH = 0.8
# Share of `max-changed-lines` above which one pass is no longer `high`.
NEAR_LIMIT = 0.75
# What one pass is judged against when no `max-changed-lines` is configured.
BIG_DIFF = 2000

# The label this action puts on the pull request, per verdict: name, colour and
# description. Created with these colours when the repository has no such label.
LABELS = {
    READY: ("codex: ready", "0e8a16", "Codex review: ready to merge"),
    NITS: ("codex: nits", "fbca04", "Codex review: mergeable, with nits"),
    CHANGES: ("codex: changes-requested", "d93f0b", "Codex review: changes requested"),
    BLOCKED: ("codex: blocked", "b60205", "Codex review: do not merge"),
}

EVENTS = ("COMMENT", "REQUEST_CHANGES")


# The answer ------------------------------------------------------------------


class Health:
    """One run's verdict, score and confidence, and how each was arrived at."""

    def __init__(self, verdict: str, score: int, confidence: str, reason: str = "",
                 deductions: list | None = None, capped: bool = False,
                 previous_score: int | None = None, previous_verdict: str = ""):
        self.verdict = verdict
        self.score = score
        self.confidence = confidence
        self.reason = reason
        self.deductions = list(deductions or [])
        self.capped = bool(capped)
        self.previous_score = previous_score
        self.previous_verdict = previous_verdict

    @property
    def headline(self) -> str:
        return INCOMPLETE_HEADLINE if self.capped else HEADLINE[self.verdict]

    @property
    def alert(self) -> str:
        return INCOMPLETE_ALERT if self.capped else ALERT[self.verdict]

    @property
    def note(self) -> str:
        return INCOMPLETE_NOTE if self.capped else ""

    @property
    def delta(self) -> int | None:
        return None if self.previous_score is None else self.score - self.previous_score

    @property
    def trend(self) -> str:
        """`55 -> 80 (+25 since last review)`, or "" with nothing to compare to."""
        if self.previous_score is None:
            return ""
        if self.delta == 0:
            return "health unchanged since the last review"
        return "health %d -> %d (%+d since last review)" % (self.previous_score, self.score, self.delta)

    def as_dict(self) -> dict:
        """Everything a later step (the check run) needs, as plain JSON."""
        return {
            "verdict": self.verdict,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "deductions": [[label, points] for label, points in self.deductions],
            "capped": self.capped,
            "previous-score": self.previous_score,
            "previous-verdict": self.previous_verdict,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Health":
        verdict = data.get("verdict") or READY
        return cls(
            verdict if verdict in ORDER else READY,
            int(data.get("score") or 0),
            (data.get("confidence") or "high"),
            data.get("reason") or "",
            [(row[0], int(row[1])) for row in (data.get("deductions") or []) if len(row) == 2],
            bool(data.get("capped")),
            data.get("previous-score"),
            data.get("previous-verdict") or "",
        )


# Confidence ------------------------------------------------------------------


def read_coverage(path: str) -> dict | None:
    """The report a full-coverage pass writes, or None when there is none.

    Shape: {"mode": "single"|"full", "complete": bool, "files_total": int,
    "files_inspected": int, "uncovered": [...], "shards": int, "passes": int}.
    """
    file = pathlib.Path(path) if path else None
    if not file or not file.is_file():
        return None
    try:
        report = json.loads(file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        print("::warning::Could not read the coverage report; using the size guard instead.")
        return None
    return report if isinstance(report, dict) else None


def coverage_confidence(report: dict) -> tuple:
    """(level, reason) from a coverage report."""
    total = max(int(report.get("files_total") or 0), 0)
    seen = max(int(report.get("files_inspected") or 0), 0)
    complete = bool(report.get("complete"))
    where = "%d/%d files inspected" % (seen, total) if total else "coverage reported"
    if total == 0:
        # Nothing left to read after the path filters: nothing was missed either.
        return "high", "no changed files in scope"
    if complete and seen >= total:
        return "high", "full coverage, " + where
    share = 1.0 if total <= 0 else min(seen / float(total), 1.0)
    if complete or share >= COMPLETE_ENOUGH:
        return "medium", "partial review, " + where
    return "low", "partial review, " + where


def size_confidence(changed: int, limit: int | None = None, incremental: bool = False) -> tuple:
    """(level, reason) with no coverage report: what the size guard already knows."""
    if incremental:
        return "medium", "incremental pass, only the commits since the last review"
    if limit:
        if changed > limit:
            return "low", "one pass over %d changed lines, over the %d line limit" % (changed, limit)
        if changed >= NEAR_LIMIT * limit:
            return "medium", "one pass over %d changed lines, close to the %d line limit" % (changed, limit)
        return "high", "one pass over the whole diff, %d changed lines" % changed
    if changed > BIG_DIFF:
        return "medium", "one pass over %d changed lines" % changed
    return "high", "one pass over the whole diff"


def confidence(coverage: dict | None = None, changed: int = 0, limit: int | None = None,
               incremental: bool = False) -> tuple:
    """(level, reason). A coverage report wins; otherwise the size guard decides."""
    if coverage:
        return coverage_confidence(coverage)
    return size_confidence(changed, limit, incremental)


# Score -----------------------------------------------------------------------


def pr_signals(raw: dict | None) -> dict:
    """The pull request's own signals, from the run's single GraphQL read.

    Only a rollup that actually failed counts: at the time the pull request is
    read this workflow is itself pending, so `PENDING` says nothing.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        "checks": (raw.get("checks") or "").upper() in ("FAILURE", "ERROR"),
        "conflicts": (raw.get("mergeable") or "").upper() == "CONFLICTING",
        "draft": bool(raw.get("draft")),
    }


def deductions(priorities: list, signals: dict | None = None) -> list:
    """(label, points) rows: what came off the starting 100, and why."""
    rows = []
    for priority in range(4):
        count = sum(1 for value in priorities if value == priority)
        if not count:
            continue
        raw = count * WEIGHT[priority]
        points = min(raw, CAP[priority])
        label = "%d P%d finding%s" % (count, priority, "" if count == 1 else "s")
        rows.append((label + (" (capped)" if points < raw else ""), points))
    for key in ("checks", "conflicts", "draft"):
        if (signals or {}).get(key):
            rows.append((SIGNAL_LABEL[key], SIGNAL_WEIGHT[key]))
    return rows


def score(rows: list) -> int:
    """100 minus every deduction, clamped to 0..100."""
    return max(0, min(100, 100 - sum(points for _, points in rows)))


# Verdict ---------------------------------------------------------------------


def from_priorities(priorities: list) -> str:
    """The rule: nothing open is ready, P2/P3 are nits, P1 asks, P0 blocks."""
    if not priorities:
        return READY
    worst = min(priorities)
    if worst <= 0:
        return BLOCKED
    if worst == 1:
        return CHANGES
    return NITS


def open_priorities(findings: list, carried: list | None = None) -> list:
    """Priorities of everything still open: reported now, or carried forward.

    A previous finding this pass did not re-check (an incremental pass, or a
    file the new commits did not touch) is still open, so it still counts.
    """
    values = [int(f.get("priority", 3)) for f in findings or []]
    values += [int(item.get("pri", 3)) for item in carried or []]
    return values


def assess(findings: list, carried: list | None = None, signals: dict | None = None,
           coverage: dict | None = None, changed: int = 0, limit: int | None = None,
           incremental: bool = False, previous: dict | None = None) -> Health:
    """Everything the review says about merging, from everything the run knows."""
    priorities = open_priorities(findings, carried)
    level, reason = confidence(coverage, changed, limit, incremental)
    rows = deductions(priorities, pr_signals(signals))
    verdict = from_priorities(priorities)
    # Confidence caps the answer: a clean or nits-only result from a review that
    # did not cover the pull request cannot claim the pull request is mergeable.
    capped = level == "low" and verdict in (READY, NITS)
    previous = previous or {}
    before = previous.get("hs")
    return Health(
        CHANGES if capped else verdict,
        score(rows),
        level,
        reason,
        rows,
        capped,
        int(before) if isinstance(before, (int, float)) else None,
        previous.get("vd") or "",
    )


# Rendering -------------------------------------------------------------------


def line(health: Health) -> str:
    """`**Changes requested** . Health 55/100 . Confidence: low (...)`."""
    parts = ["**%s**" % health.headline, "Health %d/100" % health.score,
             "Confidence: %s" % health.confidence]
    if health.reason:
        parts[-1] += " (%s)" % health.reason
    return " \u00b7 ".join(parts)


def plain_line(health: Health) -> str:
    """The same, with no Markdown, for a check run title or a log."""
    reason = " (%s)" % health.reason if health.reason else ""
    return "%s \u00b7 Health %d/100 \u00b7 Confidence: %s%s" % (
        health.headline, health.score, health.confidence, reason)


BREAKDOWN_NOTE = (
    "Per finding: P0 -40, P1 -20, P2 -5, P3 -1, capped at -80, -60, -15 and -5 in total, "
    "so a pile of nits never outranks one real bug. Pull request signals: failing checks -10, "
    "merge conflicts -10, draft -5. Findings the last review reported and this pass did not "
    "re-check still count."
)


def breakdown(health: Health) -> str:
    """"Why this score", as a collapsible block."""
    rows = ["| Signal | Effect |", "|---|---|", "| Starting score | 100 |"]
    for label, points in health.deductions:
        rows.append("| %s | -%d |" % (label, points))
    rows.append("| **Health** | **%d / 100** |" % health.score)
    return ("<details>\n<summary><b>Why this score</b></summary>\n\n"
            + "\n".join(rows)
            + "\n\n<sub>" + BREAKDOWN_NOTE + "</sub>\n\n</details>")


# Gating ----------------------------------------------------------------------


def parse_threshold(value: str) -> str | None:
    """fail-on-verdict: a verdict name, or None for no gating."""
    value = (value or "").strip().lower()
    if value in ("", "none", "off"):
        return None
    if value not in ORDER:
        raise SystemExit(
            "::error::Invalid fail-on-verdict '%s'; use ready, nits, changes-requested or blocked." % value)
    return value


def fails(health: Health, threshold: str | None) -> bool:
    """Is this verdict at or worse than the one the caller gates on?"""
    if threshold is None:
        return False
    return ORDER.index(health.verdict) >= ORDER.index(threshold)


def check_conclusion(health: Health, threshold: str | None = None) -> str:
    """The check run's conclusion for this verdict.

    Only a result the run can stand behind passes: `success` needs a clean or
    nits-only verdict *and* high confidence, so a partial review never turns a
    required check green.
    """
    if health.verdict == BLOCKED or fails(health, threshold):
        return "failure"
    if health.verdict in (READY, NITS) and health.confidence == "high":
        return "success"
    return "neutral"


def parse_event(value: str, health: Health | None = None) -> str:
    """review-event: COMMENT, REQUEST_CHANGES, or `auto` decided by the verdict."""
    setting = (value or "COMMENT").strip() or "COMMENT"
    if setting.lower() == "auto":
        wanted = health is not None and health.verdict in (CHANGES, BLOCKED) and not health.capped
        return "REQUEST_CHANGES" if wanted else "COMMENT"
    if setting.upper() not in EVENTS:
        raise SystemExit(
            "::error::Invalid review-event '%s'; use COMMENT, REQUEST_CHANGES or auto." % setting)
    return setting.upper()


# Labels ----------------------------------------------------------------------


def label_name(verdict: str) -> str:
    return LABELS[verdict][0]


def _request(call, method: str, path: str, token: str, payload: dict | None = None) -> tuple:
    """One label request: (ok, status). Labels are never worth failing a review over."""
    try:
        return True, call(method, path, token, payload)
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except (OSError, AttributeError):
            pass
        if code == 403:
            print("::warning::Cannot manage labels (403). Grant the job `issues: write` "
                  "(or `pull-requests: write`), or set labels: false.")
        elif code not in (404, 422):  # 422: the label is already there; 404: it was not on the PR
            print("::warning::Label request failed (%s: HTTP %d)." % (method, code))
        return False, code
    except (urllib.error.URLError, OSError) as error:
        print("::warning::Label request failed: %s." % error)
        return False, None


def apply_labels(repo: str, pr: str, token: str, verdict: str, call) -> str:
    """Put this verdict's label on the pull request and take the other three off.

    Returns the label applied, or "" when GitHub refused. The label is created
    with its colour first, because adding one that does not exist yet would have
    GitHub invent a colour for it.
    """
    name, colour, description = LABELS[verdict]
    ok, status = _request(call, "POST", "/repos/%s/labels" % repo, token,
                          {"name": name, "color": colour, "description": description})
    if not ok and status == 403:
        return ""
    ok, current = _request(call, "POST", "/repos/%s/issues/%s/labels" % (repo, pr), token,
                           {"labels": [name]})
    if not ok:
        return ""
    on_pr = {item.get("name") for item in (current or []) if isinstance(item, dict)}
    stale = [LABELS[key][0] for key in ORDER if LABELS[key][0] != name and LABELS[key][0] in on_pr]
    for other in stale:
        _request(call, "DELETE", "/repos/%s/issues/%s/labels/%s"
                 % (repo, pr, urllib.parse.quote(other, safe="")), token)
    print("Labelled `%s`%s." % (name, (", removed " + ", ".join("`%s`" % s for s in stale)) if stale else ""))
    return name

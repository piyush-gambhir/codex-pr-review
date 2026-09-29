#!/usr/bin/env python3
"""Write the reported findings as SARIF 2.1.0 for GitHub code scanning.

Run after publish_review.py, which writes the findings JSON:

    FINDINGS_FILE=... SARIF_FILE=codex-review.sarif sarif.py

The result is meant for `github/codeql-action/upload-sarif`, so it stays inside
what code scanning accepts: one rule per priority (P0 to P3, in that order, so
a result's ruleIndex is its priority), a level mapped from the priority, a
physical location with the finding's line range, and a partial fingerprint over
path and title so re-reviews update alerts instead of duplicating them.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from publish_review import plural, set_output  # noqa: E402

SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
TOOL_NAME = "Codex PR Review"
TOOL_URI = "https://github.com/piyush-gambhir/codex-pr-review"
FINGERPRINT_KEY = "codexPrReview/v1"
# SARIF levels and code scanning's problem.severity tag, per priority.
LEVEL = {0: "error", 1: "error", 2: "warning", 3: "note"}
SEVERITY = {0: "error", 1: "error", 2: "warning", 3: "recommendation"}
DESCRIPTION = {
    0: "Critical issue Codex reported while reviewing the pull request.",
    1: "Issue Codex reported while reviewing the pull request.",
    2: "Minor issue Codex reported while reviewing the pull request.",
    3: "Suggestion Codex made while reviewing the pull request.",
}


def rule(priority: int) -> dict:
    return {
        "id": f"codex-review/p{priority}",
        "name": f"CodexReviewP{priority}",
        "shortDescription": {"text": f"Codex review finding (P{priority})"},
        "fullDescription": {"text": DESCRIPTION[priority]},
        "defaultConfiguration": {"level": LEVEL[priority]},
        "properties": {"tags": ["codex", "review", f"priority/P{priority}"], "problem.severity": SEVERITY[priority]},
    }


def fingerprint(finding: dict) -> str:
    """Stable across re-reviews: line numbers move, path and title don't."""
    seed = "\n".join([str(finding["priority"]), finding["path"], finding["title"]])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def result(finding: dict) -> dict:
    priority = int(finding["priority"])
    start = max(int(finding["start"]), 1)
    body = (finding.get("body") or "").strip()
    text = f"P{priority}: {finding['title']}"
    if body:
        text = f"{text}\n\n{body}"
    return {
        "ruleId": f"codex-review/p{priority}",
        "ruleIndex": priority,
        "level": LEVEL.get(priority, "note"),
        "message": {"text": text},
        "locations": [{
            "physicalLocation": {
                "artifactLocation": {"uri": finding["path"].lstrip("/"), "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": start, "endLine": max(int(finding["end"]), start)},
            },
        }],
        "partialFingerprints": {FINGERPRINT_KEY: fingerprint(finding)},
    }


def sarif_log(findings: list[dict], version: str = "", automation_id: str = "") -> dict:
    driver = {"name": TOOL_NAME, "informationUri": TOOL_URI, "rules": [rule(p) for p in range(4)]}
    if version:
        driver["version"] = version
    run = {"tool": {"driver": driver}, "results": [result(f) for f in findings]}
    if automation_id:
        run["automationDetails"] = {"id": automation_id}
    return {"version": "2.1.0", "$schema": SCHEMA, "runs": [run]}


def main() -> int:
    env = os.environ
    target = env.get("SARIF_FILE", "").strip()
    if not target:
        return 0
    findings_file = env.get("FINDINGS_FILE", "").strip()
    findings: list[dict] = []
    if findings_file and pathlib.Path(findings_file).is_file():
        findings = json.loads(pathlib.Path(findings_file).read_text(encoding="utf-8"))
    else:
        print(f"::warning::No findings file to convert to SARIF ({findings_file or 'unset'}); writing an empty run.")

    title = env.get("REVIEW_TITLE", "").strip() or TOOL_NAME
    log = sarif_log(findings, env.get("CODEX_VERSION", "").strip(), f"{title}/")
    path = pathlib.Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    set_output("sarif-file", str(path))
    print(f"Wrote {plural(len(findings), 'finding')} to {path} as SARIF 2.1.0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

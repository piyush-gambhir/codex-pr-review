# Security notes

## CodeQL checkout triage (2026-10-29)

The reusable review workflow checks out its own action at GitHub's trusted
`job.workflow_repository` and `job.workflow_sha`, and loads review guidelines
from the repository's default branch. Those refs are not supplied by the PR.
The checkout alerts on those trusted sources are false positives.

The separate checkout of the PR head is intentional: reviewing a contribution
requires reading its untrusted source. The action implementation comes from the
trusted checkout. A PR must not be allowed to replace that implementation or
the default-branch guidelines. This checkout remains a reviewed trust boundary,
not a claim that contributed code is safe to execute. Keep the review sandbox
and token permissions restricted. No checkout alert is globally suppressed.

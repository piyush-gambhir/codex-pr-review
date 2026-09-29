# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

- **Adoption is a few lines.** New reusable workflow `piyush-gambhir/codex-pr-review/.github/workflows/review.yml@v1` bundles the trigger and the review, so a consuming repository writes a `uses:` line, `base-branches` and `secrets: inherit` instead of copying 180 lines of workflow.
- **Trigger sub-action** `piyush-gambhir/codex-pr-review/trigger@v1` (`scripts/trigger.py`): parses `@gpt review [provider] [effort]`, checks write access, open PR, same-repo head and allowed base branches, reacts with 👀, and outputs `run`, `pr-number`, `head-sha`, `base-ref`, `provider`, `effort`, `comment-id` and `reason`. Use it on its own to keep a custom review job.
- **Two more triggers**: adding a label (`codex-review` by default, removed again so re-adding re-runs the review) and `workflow_dispatch` with a pull request number.
- Several base branches (`base-branches: main,develop`), a configurable command, `allowed-providers`, and `runs-on` for custom or self-hosted runners.
- The full workflow is still available as [`examples/codex-review-standalone.yml`](examples/codex-review-standalone.yml), which is also what to use on GitHub Enterprise Server.

## v1.0.0 (2026-09-30)

First public release.

- Codex's native reviewer (`codex exec review`) on OpenAI models via the OpenAI API (`provider: openai`) or Amazon Bedrock with GitHub OIDC (`provider: bedrock`).
- Review layout: verdict, issues table with `file:lines` links pinned to the reviewed commit, inline comments on diff lines, collapsible details for findings outside the diff, meta line and next-step hints.
- Progress note replaced by the review; failure note with the real error; rocket or confused reaction on the requesting comment.
- Earlier Codex comments collapsed as outdated on re-review.
- Repository guidelines (`review-instructions`, `review-instructions-file`), `max-priority` filter, `fail-on-priority` gating, `post-mode` (`review`, `comment`, `none`), outputs and job summary.
- Custom bot identity through a GitHub App token.
- Example workflow with per-request provider and effort (`@gpt review bedrock high`).

# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

- Real token usage and a cost estimate on every review. Codex reports zero usage in review mode, so [`scripts/usage.py`](scripts/usage.py) reads the counts from the session rollout Codex writes in `CODEX_HOME` (the review no longer runs `--ephemeral`) and prices them from a built-in table, long-context requests included. The meta line gains `36,615 input (29,312 cached) + 926 output tokens · ~$0.03`.
- New outputs `input-tokens`, `cached-input-tokens`, `output-tokens` and `estimated-cost-usd`.
- New input `pricing` to price a model the built-in table doesn't know.

## v1.0.0 (2026-09-30)

First public release.

- Codex's native reviewer (`codex exec review`) on OpenAI models via the OpenAI API (`provider: openai`) or Amazon Bedrock with GitHub OIDC (`provider: bedrock`).
- Review layout: verdict, issues table with `file:lines` links pinned to the reviewed commit, inline comments on diff lines, collapsible details for findings outside the diff, meta line and next-step hints.
- Progress note replaced by the review; failure note with the real error; rocket or confused reaction on the requesting comment.
- Earlier Codex comments collapsed as outdated on re-review.
- Repository guidelines (`review-instructions`, `review-instructions-file`), `max-priority` filter, `fail-on-priority` gating, `post-mode` (`review`, `comment`, `none`), outputs and job summary.
- Custom bot identity through a GitHub App token.
- Example workflow with per-request provider and effort (`@gpt review bedrock high`).

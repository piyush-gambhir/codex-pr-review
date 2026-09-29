# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

- Re-review awareness: every posted review carries a hidden state marker (reviewed commit plus a fingerprint per finding), so the next review lists what is gone under "Resolved since last review", counts it in the verdict line, and tags findings reported again as "still open".
- Inline threads of fixed findings are resolved on GitHub (`resolve-fixed-threads`, default `true`; best effort, never fails the review).
- New `incremental` input (default `false`): review only the commits pushed since the last Codex review, noted in the meta line. Findings in files those commits did not touch are carried forward as "still open, not re-checked" instead of counted as fixed, and a force-push falls back to a full review.
- New output `resolved-count`; `findings-file` entries gained `fingerprint`.

## v1.0.0 (2026-09-30)

First public release.

- Codex's native reviewer (`codex exec review`) on OpenAI models via the OpenAI API (`provider: openai`) or Amazon Bedrock with GitHub OIDC (`provider: bedrock`).
- Review layout: verdict, issues table with `file:lines` links pinned to the reviewed commit, inline comments on diff lines, collapsible details for findings outside the diff, meta line and next-step hints.
- Progress note replaced by the review; failure note with the real error; rocket or confused reaction on the requesting comment.
- Earlier Codex comments collapsed as outdated on re-review.
- Repository guidelines (`review-instructions`, `review-instructions-file`), `max-priority` filter, `fail-on-priority` gating, `post-mode` (`review`, `comment`, `none`), outputs and job summary.
- Custom bot identity through a GitHub App token.
- Example workflow with per-request provider and effort (`@gpt review bedrock high`).

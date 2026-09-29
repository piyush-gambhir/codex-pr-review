# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

### Added

- Suggested fixes (`suggestions`, on by default): Codex is asked to attach the full replacement for the lines it flagged as a fenced `suggestion` block. Inline comments pass it through as a committable GitHub suggestion only when the comment anchors exactly those lines; everywhere else it is rendered as a plain "Suggested fix" code block. Findings with a fix are marked in the issues table, and `findings-file` gains a `suggestion` field.
- Path filters `include-paths` and `exclude-paths` (glob patterns, newline or comma separated, `**` supported). Findings outside the filter are dropped, reported as the new `path-filtered-count` output and noted under the review; exclusions are also passed to Codex so it skips those files.
- Large-PR guard `max-changed-lines` with `large-pr` (`warn` or `skip`). The changed-line count is taken between the merge base and HEAD before Codex runs and exposed as the new `changed-lines` output; `skip` posts a short note and finishes without calling Codex.

### Fixed

- Finding explanations are dedented as a block instead of line by line, so fenced code inside them keeps its indentation.

## v1.0.0 (2026-09-30)

First public release.

- Codex's native reviewer (`codex exec review`) on OpenAI models via the OpenAI API (`provider: openai`) or Amazon Bedrock with GitHub OIDC (`provider: bedrock`).
- Review layout: verdict, issues table with `file:lines` links pinned to the reviewed commit, inline comments on diff lines, collapsible details for findings outside the diff, meta line and next-step hints.
- Progress note replaced by the review; failure note with the real error; rocket or confused reaction on the requesting comment.
- Earlier Codex comments collapsed as outdated on re-review.
- Repository guidelines (`review-instructions`, `review-instructions-file`), `max-priority` filter, `fail-on-priority` gating, `post-mode` (`review`, `comment`, `none`), outputs and job summary.
- Custom bot identity through a GitHub App token.
- Example workflow with per-request provider and effort (`@gpt review bedrock high`).

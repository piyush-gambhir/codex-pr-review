# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

### Added

- **Adoption is a few lines.** New reusable workflow `piyush-gambhir/codex-pr-review/.github/workflows/review.yml@v1` bundles the trigger and the review, so a consuming repository writes a `uses:` line, `base-branches` and `secrets: inherit` instead of copying 180 lines of workflow.
- **Trigger sub-action** `piyush-gambhir/codex-pr-review/trigger@v1` (`scripts/trigger.py`): parses `@gpt review [provider] [effort]`, checks write access, open PR, same-repo head and allowed base branches, reacts with 👀, and outputs `run`, `pr-number`, `head-sha`, `base-ref`, `provider`, `effort`, `comment-id` and `reason`. Use it on its own to keep a custom review job.
- **Two more triggers**: adding a label (`codex-review` by default, removed again so re-adding re-runs the review) and `workflow_dispatch` with a pull request number.
- Several base branches (`base-branches: main,develop`), a configurable command, `allowed-providers`, and `runs-on` for custom or self-hosted runners.
- The full workflow is still available as [`examples/codex-review-standalone.yml`](examples/codex-review-standalone.yml), which is also what to use on GitHub Enterprise Server.
- Real token usage and a cost estimate on every review. Codex reports zero usage in review mode, so [`scripts/usage.py`](scripts/usage.py) reads the counts from the session rollout Codex writes in `CODEX_HOME` (the review no longer runs `--ephemeral`) and prices them from a built-in table, long-context requests included. The meta line gains `36,615 input (29,312 cached) + 926 output tokens · ~$0.03`.
- New outputs `input-tokens`, `cached-input-tokens`, `output-tokens` and `estimated-cost-usd`.
- New input `pricing` to price a model the built-in table doesn't know.
- `check-run` (default `false`, needs `checks: write`): a check run on the reviewed commit, in progress while Codex reviews and completed with `success`, `neutral`, `failure` or `cancelled`, the verdict and issues table as its output, and one annotation per finding (`failure` for P0/P1, `warning` for P2, `notice` for P3) sent 50 at a time. A missing permission only warns.
- `sarif-file`: the findings as SARIF 2.1.0 (one rule per priority, levels from the priority, fingerprints over path and title) for `github/codeql-action/upload-sarif`, plus a `sarif-file` output. Written even when `fail-on-priority` fails the step.
- New `check-run-id` output.
- Re-review awareness: every posted review carries a hidden state marker (reviewed commit plus a fingerprint per finding), so the next review lists what is gone under "Resolved since last review", counts it in the verdict line, and tags findings reported again as "still open".
- Inline threads of fixed findings are resolved on GitHub (`resolve-fixed-threads`, default `true`; best effort, never fails the review).
- New `incremental` input (default `false`): review only the commits pushed since the last Codex review, noted in the meta line. Findings in files those commits did not touch are carried forward as "still open, not re-checked" instead of counted as fixed, and a force-push falls back to a full review.
- New output `resolved-count`; `findings-file` entries gained `fingerprint`.
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

# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

### Changed

- **No emoji anywhere.** Everything the action posts or writes now uses GitHub's own visual language instead: the verdict, the progress note, the failure note, the skip note and the check run summary are [alert blocks](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#alerts), so GitHub draws the icon, the colour and the border. The verdict type follows the worst priority reported: `CAUTION` for P0 or P1, `WARNING` for P2, `NOTE` for P3 only, `TIP` when the diff is clean. A progress note is a `NOTE`, a failure a `CAUTION` with the error in a code block, a skip a `WARNING`.
- The priority dots, speech balloon, down arrow, light bulb, check marks, magnifier, cross mark and skip symbol are replaced by the 16px [Octicons](icons/) in the new `icons/` directory (MIT, from `@primer/octicons` 19.38.0), each recoloured for what it means. P0 to P3 are four different glyphs rather than one glyph in four colours. Every colour clears 3:1 WCAG contrast against both GitHub's light (`#ffffff`) and dark (`#0d1117`) comment backgrounds, so no `prefers-color-scheme` switching is needed; [`icons/README.md`](icons/README.md) lists the measured ratios.
- The issues table names its last column **Where** and says `Inline` or `Below` in words next to the icon, instead of an emoji. Every `<img>` carries meaningful `alt` text and an explicit 16x16 size, so a review reads correctly before or without the images.

### Added

- New input `icons` (default `true`). `false` renders every layout as words with no external images at all, for GitHub Enterprise Server or an organisation that blocks GitHub's image proxy.
- New input `icon-base-url` to serve the images from somewhere else, such as an internal mirror.
- New module [`scripts/icons.py`](scripts/icons.py) (the catalogue and the URL resolution) and the development script [`scripts/dev/build_icons.py`](scripts/dev/build_icons.py), which regenerates `icons/` from the Octicons npm package and refuses to write a colour that fails the contrast check.
- The reusable workflow passes `icons` and `icon-base-url` through.

### Release checklist

- The default image URL is jsDelivr for this repository at the ref the action was resolved from, but only when that is the public `piyush-gambhir/codex-pr-review` at a tag or a full commit SHA. A local `./` checkout, a private copy of this repository or a branch ref falls back to `PINNED_REF` in [`scripts/icons.py`](scripts/icons.py). **Bump `PINNED_REF` to the tag being released whenever `icons/` changes, before tagging**; `tests/test_icons.py` checks it is a tag or a SHA, never a branch.

## v1.2.1 (2026-09-30)

### Removed

- The project icon (`assets/`) and the Marketplace branding icons in `action.yml` and `trigger/action.yml`. The bot's avatar comes from the GitHub App you create; the README explains where to set it.

## v1.2.0 (2026-09-30)

### Changed

- Codex is installed with **pnpm** (`pnpm/action-setup@v6`) on the latest **Node.js LTS** (`actions/setup-node@v7`). New inputs `node-version` (default `lts/*`, empty keeps the runner's Node) and `pnpm-version` (default `12`). With `codex-version: latest`, pnpm resolves the newest release older than its minimum release age.
- Workflows and examples use the latest actions: `actions/checkout@v7`, `actions/github-script@v9`, `actions/create-github-app-token@v3`.
- GitHub App identity now uses the app's client ID (`create-github-app-token` v3 deprecated `app-id`): variable `CODEX_REVIEW_APP_CLIENT_ID`, and the reusable workflow input `app-id` is now `app-client-id`.

## v1.1.1 (2026-09-30)

### Changed

- Codex CLI pin bumped to 0.159.2 (verified end to end: parsing, usage, re-review tracking, suggestions and SARIF unchanged). `codex-version: latest` is documented for always-newest installs.

## v1.1.0 (2026-09-30)

### Added

- **Adoption is a few lines.** New reusable workflow `piyush-gambhir/codex-pr-review/.github/workflows/review.yml@v1` bundles the trigger and the review, so a consuming repository writes a `uses:` line, `base-branches` and `secrets: inherit` instead of copying 180 lines of workflow.
- **Trigger sub-action** `piyush-gambhir/codex-pr-review/trigger@v1` (`scripts/trigger.py`): parses `@gpt review [provider] [effort]`, checks write access, open PR, same-repo head and allowed base branches, reacts with an eyes reaction, and outputs `run`, `pr-number`, `head-sha`, `base-ref`, `provider`, `effort`, `comment-id` and `reason`. Use it on its own to keep a custom review job.
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

- GitHub API calls retry dropped connections, 5xx and 429 with backoff. Posting a review only retries when GitHub reports it did not act (429, 503), so a review is never posted twice.
- Reviews with a single finding (headed "Review comment:") are parsed instead of shown as clean.
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

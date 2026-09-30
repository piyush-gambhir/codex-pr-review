# Changelog

This project follows [semantic versioning](https://semver.org). The `v1` tag always points to the latest 1.x release; breaking input or output changes get a new major version.

## Unreleased

Reviews that cover the whole pull request, say how much of it they read, and answer whether to merge it.

### Added

- **Full reviews.** New input `review-mode`: `single` (one pass over the diff, the default and exactly what it did before), `full` (one pass per shard of the diff plus a cross-cutting pass), or `auto` (full over `max-changed-lines`, single below it). `large-pr: full` says the same thing from the size guard's side, `@gpt review full` asks for one run, and the resolved mode is an action output and part of the settings digest, so a single review never satisfies a later request for a full one.
- **Shards that keep the whole pull request in view** ([`scripts/shards.py`](scripts/shards.py)). The diff is cut into shards of about `shard-lines` changed lines (default 2500), keeping directories together, never splitting a file and packing tests after the source they belong with, in a deterministic order. Each shard is reviewed in a checkout of the full pull request head with `--base` pointing at a synthetic commit: HEAD's tree with only that shard's files put back to their merge-base versions (added files removed, deleted files restored, renames undone). `git diff` between the two is exactly the shard, while every other file is present at its final state, so Codex reads real callers rather than a slice of a diff. The commits are built with `read-tree`, `update-index`, `write-tree` and `commit-tree` against a temporary index file: no ref is created and neither the working tree nor the repository's index is touched. New inputs `shard-lines` and `max-shards` (default 24, so a bigger pull request gets bigger shards rather than more model calls).
- **Passes run concurrently** ([`scripts/full_review.py`](scripts/full_review.py)), up to `max-parallel` (default 4) at a time, each with its own `CODEX_HOME` so their session rollouts never collide, each in a reused worktree of the pull request head. New inputs `pass-timeout-minutes` (default 10) and `review-budget-minutes` (default 20); a pass that runs out of time, fails, or never starts is reported as uncovered and named under the review, never silently dropped. Every pass's findings are then merged, de-duplicated on the fingerprint re-reviews already use (keeping the worst priority and any suggested fix) and written back out in Codex's own review layout, so publishing a full review and publishing a single one are the same code path.
- **A follow-up pass and a cross-cutting pass.** Any changed file no pass actually read gets a shard of its own; then the whole diff is reviewed once more with the per-area summaries in its instructions, asked only for issues that span areas: an API or contract change against callers that were not changed with it, wiring, authentication applied to some paths but not others, migrations against the code that reads those columns.
- **Measured coverage** ([`scripts/coverage.py`](scripts/coverage.py)). Both modes now write `$RUNNER_TEMP/codex-review-coverage.json` with `{"mode", "complete", "files_total", "files_inspected", "uncovered", "shards", "passes"}`, show it in the meta line under the review (`Coverage 224/224 files (full, 12 passes)`) and publish it as the outputs `coverage-files-total`, `coverage-files-inspected` and `coverage-complete`. It is read out of the session rollouts Codex wrote, not assumed: a changed file counts as inspected when Codex read the file itself (`cat`, `sed -n`, `nl`, a file-reading tool call) or read a diff whose recorded output contained it, so a diff truncated before it reached a file counts only as far as it got, and listings and searches (`ls`, `rg`, `git diff --stat`) do not count at all. "Inspected" is an honest floor on the review's scope, not a promise that every bug was found.
- The trigger action gained a `full` input and a `full` output, and the reusable workflow gained `review-mode`, `max-changed-lines`, `shard-lines` and `timeout-minutes` inputs.
- The end-to-end harness takes `FULL=1` and `SHARD_LINES=...`, so a small pull request can be made to shard for real.
- **Every review says whether the pull request should be merged.** The headline is now a merge verdict with a health score and a confidence level: `Changes requested · Health 55/100 · Confidence: low (partial review, 38/224 files inspected)`. All three are computed in the action ([`scripts/verdict.py`](scripts/verdict.py)), not asked of the model: Codex's own `overall_correctness` and `overall_confidence_score` fields come back empty and `0.0` on the pinned versions, and a rule you can read beats a number you cannot check. The verdict is advisory and the README says so: it belongs alongside CI and a human approval, not instead of them.
  - **Verdict** from the worst finding still open: `ready` (**Ready to merge**, `TIP`), `nits` (**Mergeable, with nits**, `NOTE`), `changes-requested` (**Changes requested**, `WARNING`), `blocked` (**Do not merge**, `CAUTION`). Findings the last review reported and this pass did not re-check count as open, so an incremental re-review of one file can no longer say "ready to merge" over an untouched P0 somewhere else.
  - **Health score** out of 100: -40 per P0, -20 per P1, -5 per P2, -1 per P3, capped at -80, -60, -15 and -5 in total, so every nit in the world costs at most 20, which is exactly one P1. Failing checks -10, merge conflicts -10 and a draft pull request -5 come off as well, read from the run's existing single GraphQL call ([`scripts/pr_state.py`](scripts/pr_state.py)) rather than any new request; only a rollup that actually failed counts, because at that point the review's own workflow is still pending. A collapsed **Why this score** block lists every row.
  - **Trend**: the score and verdict travel in the review's state marker, so the next review says `Health 45 -> 70 (+25 since last review)`.
  - **Confidence** `high`, `medium` or `low`, from the coverage report a full-coverage pass writes (`COVERAGE_FILE`) or, without one, from the size guard and whether the pass was incremental. Low confidence caps the verdict: a clean or nits-only result the run cannot stand behind is reported as **Needs a full review** with a sentence saying why, rather than as ready or mergeable. The score is not capped, because it grades the code that was read while the confidence grades the reading.
- **The pull request is labelled with the verdict**: `codex: ready`, `codex: nits`, `codex: changes-requested` or `codex: blocked`, created with sensible colours when the repository has none, with the other three removed. New input `labels` (default `true`). It needs `issues: write`; without it the action warns once and posts the review as usual.
- New input `fail-on-verdict` (`ready`, `nits`, `changes-requested`, `blocked`), which fails the step and the check run on that verdict or worse. It works alongside `fail-on-priority`, and both still leave the review posted.
- New input `review-event`: `COMMENT` (the default, unchanged), `REQUEST_CHANGES`, or `auto`, which requests changes on a `changes-requested` or `blocked` verdict. GitHub may refuse to let the posting identity request changes; the action then posts the same review as a comment and warns, and gives up the inline anchors only if that is refused too. `auto` never requests changes over a **Needs a full review** verdict, since nothing was actually found.
- New outputs `verdict`, `health-score`, `confidence`, `health-trend` and `label`. The reusable workflow passes `fail-on-verdict`, `labels` and `review-event` through and logs the verdict.

### Changed

- `scripts/usage.py` sums every pass's rollout, from the homes listed in `CODEX_HOMES_FILE`, so a full review's cost is the cost of all of it. Requests are now de-duplicated per rollout file rather than globally, so two passes that happen to use the same tokens are billed as two requests.
- The re-run hint offers `full` alongside `force`, `high` and `bedrock`.
- The check run's conclusion follows the verdict: `success` only for a `ready` or `nits` verdict at `high` confidence, `failure` for `blocked` or either gate, `neutral` otherwise. A partial review can no longer turn a required check green, which the old "no findings is a pass" rule allowed.
- The "already reviewed" note repeats the verdict the review it points at reached.
- The review job in the reusable workflow and both examples now ask for `issues: write`, for the label.

## v1.3.0 (2026-09-30)

Faster and cheaper reviews, and a GitHub-native look with no emoji.

### Added

- **The same commit is not reviewed twice.** Every posted review's state marker now carries a digest of the settings that produced it (provider, model, effort, base ref, guidelines, path filters, priority cut-off, post mode), so a request for a commit that already has a matching review posts a short note linking to it and calls no model. New inputs `skip-unchanged` (default `true`) and `force`; `@gpt review force` in a comment, a `force` input on the trigger action and the reusable workflow, and a `force` output on the trigger. New outputs `skipped`, `skip-reason` and `existing-review-url`. Nothing is installed or posted before the decision, and anything that could change the answer stops the skip, including a marker written before this release.
- **The Codex CLI is cached** with `actions/cache/restore@v6` and `actions/cache/save@v6` (saved as soon as the install exists, so a failed or cancelled review still leaves the cache warm), keyed on the runner's OS and architecture, the resolved Codex version and the pnpm major, restoring `RUNNER_TEMP/codex-cli` (install scripts stay disabled). With a pinned `codex-version` the key is known before anything is set up, so a warm cache skips `actions/setup-node`, `pnpm/action-setup` and the install: about 14 s of install work becomes about 2 s on `ubuntu-24.04`. `codex-version: latest` is resolved with pnpm first, so the key always names the version that gets installed; an unresolvable version leaves the cache out of that run rather than risking the wrong one. A restored install is used only when it runs and reports that version. New input `cache-install` (default `true`).
- **`actions/setup-node` and `pnpm/action-setup` are skipped when the runner already has what the install needs**: `node-version: ''` as before, and now an open request such as `lts/*` that the runner's Node already satisfies. pnpm is only kept when Node is kept too.
- New input `icons` (default `true`). `false` renders every layout as words with no external images at all, for GitHub Enterprise Server or an organisation that blocks GitHub's image proxy.
- New input `icon-base-url` to serve the images from somewhere else, such as an internal mirror.
- New module [`scripts/icons.py`](scripts/icons.py) (the catalogue and the URL resolution) and the development script [`scripts/dev/build_icons.py`](scripts/dev/build_icons.py), which regenerates `icons/` from the Octicons npm package and refuses to write a colour that fails the contrast check.
- The reusable workflow passes `icons` and `icon-base-url` through.

### Changed

- **A newer request cancels the review in flight.** The reusable workflow and the standalone example set `cancel-in-progress: true` on the per-pull-request concurrency group. A cancelled run removes its own progress note and posts no failure note.
- **One read of the pull request per run instead of four.** The state marker, the comments to collapse and the threads to resolve now come from a single GraphQL query ([`scripts/pr_state.py`](scripts/pr_state.py)) and travel to the later steps in the plan file: six conversation listings per run become two. REST remains the fallback for an old GitHub Enterprise Server, a token GraphQL refuses, or a conversation longer than one page. The trigger also takes the default branch from the pull request payload instead of fetching the repository.
- The previous review is read before the progress note is posted, so a run never lists its own note.
- `fetch-depth: 0` stays, with the reasons documented under "Checkout cost" in the README: a `filter: blob:none` partial clone is much cheaper but fetches blobs lazily, and `git diff` against the merge base then fails once `persist-credentials: false` has removed the credential.
- **No emoji anywhere.** Everything the action posts or writes now uses GitHub's own visual language instead: the verdict, the progress note, the failure note, the skip note and the check run summary are [alert blocks](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#alerts), so GitHub draws the icon, the colour and the border. The verdict type follows the worst priority reported: `CAUTION` for P0 or P1, `WARNING` for P2, `NOTE` for P3 only, `TIP` when the diff is clean. A progress note is a `NOTE`, a failure a `CAUTION` with the error in a code block, a skip a `WARNING`.
- The priority dots, speech balloon, down arrow, light bulb, check marks, magnifier, cross mark and skip symbol are replaced by the 16px [Octicons](icons/) in the new `icons/` directory (MIT, from `@primer/octicons` 19.38.0), each recoloured for what it means. P0 to P3 are four different glyphs rather than one glyph in four colours. Every colour clears 3:1 WCAG contrast against both GitHub's light (`#ffffff`) and dark (`#0d1117`) comment backgrounds, so no `prefers-color-scheme` switching is needed; [`icons/README.md`](icons/README.md) lists the measured ratios.
- The issues table names its last column **Where** and says `Inline` or `Below` in words next to the icon, instead of an emoji. Every `<img>` carries meaningful `alt` text and an explicit 16x16 size, so a review reads correctly before or without the images.

### Fixed

- A finding only counts as resolved when its file changed since the last reviewed commit. Codex isn't deterministic, so on an identical commit a reworded or dropped finding was reported as resolved and its thread closed; such findings are now listed as still open.

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

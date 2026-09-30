<h1 align="center">Codex PR Review</h1>

<p align="center">On-demand pull request reviews from <b>Codex's native reviewer</b>, on OpenAI models via the <b>OpenAI API</b> or <b>Amazon Bedrock</b>.</p>

Comment `@gpt review` on a pull request. About a minute later you get a review with a verdict, an issues table linking every finding to its exact lines, and inline comments on the diff. The review uses `codex exec review`, the same logic as `/review` in the Codex CLI, so you get OpenAI's own review behaviour rather than a hand-written prompt.

| `provider` | Auth | Billing | Default model |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` secret | OpenAI API | `gpt-6.1-sol` |
| `bedrock` | IAM role via GitHub OIDC, no stored keys | Your AWS account, at OpenAI's rates | `us.openai.gpt-6-sol` |

> This is an independent project, not an official OpenAI or AWS product.

## What a review looks like

No emoji anywhere: the verdict is a GitHub alert block, which GitHub renders with its own icon, colour and border, and the markers are 16px [Octicons](icons/) served over a CDN. This is the review body, rendered exactly as it is posted:

> [!CAUTION]
> **5 issues to address** (4 P1, 1 P2) · 2 resolved

The patch introduces five correctness issues, including incorrect discounts and non-integer monetary results.

|  | Priority | Issue | Location | Where |
|---|---|---|---|---|
| <img src="icons/priority-p1.svg" width="16" height="16" alt="High"> | P1 | Convert percentage points to a fraction <img src="icons/suggestion.svg" width="16" height="16" alt="Suggested fix"> | `e2e/pricing.ts:16` | <img src="icons/inline.svg" width="16" height="16" alt="Commented inline on the diff"> Inline |
| <img src="icons/priority-p1.svg" width="16" height="16" alt="High"> | P1 | Round discounted totals to integer cents | `e2e/pricing.ts:16-17` | <img src="icons/inline.svg" width="16" height="16" alt="Commented inline on the diff"> Inline |
| <img src="icons/priority-p1.svg" width="16" height="16" alt="High"> | P1 | Handle zero units before calculating the average <img src="icons/still-open.svg" width="16" height="16" alt="Reported again"> | `e2e/pricing.ts:22` | <img src="icons/inline.svg" width="16" height="16" alt="Commented inline on the diff"> Inline |
| <img src="icons/priority-p1.svg" width="16" height="16" alt="High"> | P1 | Round average unit prices to integer cents | `e2e/pricing.ts:22` | <img src="icons/inline.svg" width="16" height="16" alt="Commented inline on the diff"> Inline |
| <img src="icons/priority-p2.svg" width="16" height="16" alt="Medium"> | P2 | Sort ascending to select the cheapest item | `e2e/pricing.ts:26` | <img src="icons/outside.svg" width="16" height="16" alt="Reported in the review body"> Below |

**Resolved since last review (2)**

- <img src="icons/resolved.svg" width="16" height="16" alt="Resolved"> ~~Guard against a missing currency code~~ · `e2e/pricing.ts:9`
- <img src="icons/resolved.svg" width="16" height="16" alt="Resolved"> ~~Reject a negative quantity~~ · `e2e/cart.ts:31`

<sub>Reviewed `314c52a` against `main` · `gpt-6.1-sol` via OpenAI API · medium effort · 36,615 input (29,312 cached) + 926 output tokens · ~$0.03 · View run</sub><br>
<sub>Re-run: `@gpt review` · deeper: `@gpt review high` · on Bedrock: `@gpt review bedrock`</sub>

- **Verdict first**, as the alert block GitHub already uses on its own pages: `CAUTION` when anything is P0 or P1, `WARNING` for P2, `NOTE` for P3 only, `TIP` when the diff is clean, with the count per priority and how many findings are gone since the last review.
- **Issues table**: every finding with its priority (P0 to P3, a different glyph each), a `file:lines` link pinned to the reviewed commit, and whether it was commented on inline or written up in the body.
- **Suggested fixes**: when a fix is a small replacement of the flagged lines, it arrives as a committable GitHub suggestion, marked <img src="icons/suggestion.svg" width="16" height="16" alt="Suggested fix"> in the table. Anywhere the comment can't be anchored to exactly those lines it becomes a plain "Suggested fix" code block instead, so a fix is never applied to the wrong place.
- **Inline comments** on the exact diff lines (including multi-line ranges); findings outside the diff appear as collapsible details, so nothing is lost.
- **Progress and failures on the PR**: a `NOTE` "in progress" note (with a link to the live run) is replaced by the review. If anything fails, it becomes a `CAUTION` "failed" note with the actual error in a code block. The requesting comment is reacted to: eyes when the request is accepted, then rocket or confused.
- **Re-reviews stay tidy**: earlier Codex comments are collapsed as outdated, fixed findings are listed as resolved and their inline threads are resolved on GitHub.
- **Next steps** under every review: how to re-run, go deeper, or switch provider.
- **What it cost**: real token counts and a cost estimate on the meta line, also available as outputs.
- **Text only if you prefer**: `icons: false` renders every layout in words, with no external images at all.

## Quick start

1. Add an `OPENAI_API_KEY` Actions secret (or set up [Bedrock](#amazon-bedrock)).
2. Put this in `.github/workflows/codex-review.yml` on your default branch (comment-triggered workflows only run from there):

   ```yaml
   name: Codex PR Review

   on:
     issue_comment:
       types: [created]
     pull_request:
       types: [labeled]

   permissions: {}

   jobs:
     codex:
       permissions:
         contents: read
         pull-requests: write # post the review, react and reply
         issues: write # react to the requesting comment
         id-token: write # AWS OIDC, for the bedrock provider
       uses: piyush-gambhir/codex-pr-review/.github/workflows/review.yml@v1
       with:
         base-branches: main
       secrets: inherit
   ```

3. Comment on an open pull request:

   | Comment | Does |
   |---|---|
   | `@gpt review` | Review with the defaults |
   | `@gpt review high` | Deeper reasoning (`low`, `medium`, `high`, `xhigh`) |
   | `@gpt review bedrock` | Use Amazon Bedrock for this request |
   | `@gpt review bedrock xhigh` | Both |
   | `@gpt review force` | Review a commit that already has a review |

Only open, same-repository PRs into `base-branches`, requested by someone with write access, are reviewed, one review at a time per PR. Nothing runs on open or push, so you only pay for reviews someone asks for. Asking twice for the same commit does not pay twice: the second request finds the review that is already there and says so (see [Not paying twice](#not-paying-twice)).

[`examples/codex-review.yml`](examples/codex-review.yml) is this file with the manual trigger and the optional settings filled in. If you would rather see every step (or you are on GitHub Enterprise Server, where the reusable workflow does not work), copy [`examples/codex-review-standalone.yml`](examples/codex-review-standalone.yml) instead: same behaviour, all of it in your repository.

## Triggers

Three ways to ask for a review, all handled by the same gate:

| Trigger | How | Notes |
|---|---|---|
| Comment | `@gpt review [openai\|bedrock] [low\|medium\|high\|xhigh] [full] [force]` at the start of a PR comment | Options come from the first line; `full` reviews every changed file (see [Full reviews](#full-reviews)); the comment gets an eyes reaction, then rocket (answered) or confused (failed) |
| Label | Add the `codex-review` label to the PR | The label is removed again, so re-adding it re-runs the review. Set `label: ""` to switch this off |
| Manual | Actions tab, the workflow, **Run workflow**, then the PR number | Add a `workflow_dispatch` input named `pr-number` and pass it through as `pr-number` |

Every trigger requires write access on the repository, an open pull request, a same-repository head branch (fork PRs are skipped, because the review job holds model credentials) and a base branch listed in `base-branches`. A PR into an unlisted base branch gets one short reply saying so; everything else is declined silently, and the workflow stays green.

## Reusable workflow inputs

Everything is optional. `base-branches` defaults to your repository's default branch.

| Input | Default | Description |
|---|---|---|
| `base-branches` | default branch | Comma-separated base branches that may be reviewed |
| `command` | `@gpt review` | Comment prefix that requests a review |
| `skip-unchanged` | `true` | Skip a re-review of a commit that already has one with the same settings |
| `force` | `false` | Make label and manual runs review even when nothing changed |
| `review-mode` | `single` | `single`, `full` or `auto`; `@gpt review full` asks for one run |
| `max-changed-lines` | | Size above which `review-mode: auto` reviews shard by shard |
| `shard-lines` | `2500` | Changed lines per shard in a full review |
| `timeout-minutes` | `30` | Job timeout; raise it for full reviews |
| `label` | `codex-review` | Label that requests a review; empty turns label triggers off |
| `remove-label` | `true` | Remove the label again, so it can be re-added to re-run |
| `default-provider` | `openai` | Provider when the request does not name one |
| `default-effort` | `medium` | Reasoning effort when the request does not name one |
| `allowed-providers` | `openai,bedrock` | Providers a request may choose |
| `pr-number` | | Pull request number for `workflow_dispatch` runs |
| `runs-on` | `ubuntu-24.04` | Runner label, or a JSON array such as `["self-hosted", "linux"]` |
| `model` | per provider | Model ID |
| `aws-region` | `us-east-1` | Bedrock region |
| `guidelines-path` | `.github/codex-review.md` | Guidelines file, read from your default branch; empty loads none |
| `max-priority` | `P3` | Lowest priority to report |
| `fail-on-priority` | | Fail the review when a finding at this priority or higher is reported |
| `icons` | `true` | Show the icon images; `false` renders every layout as text only |
| `icon-base-url` | jsDelivr | Base URL the icon images are loaded from |
| `app-client-id` | | GitHub App client ID, to post under your own bot name and avatar |

Secrets (`secrets: inherit` passes whichever you have): `OPENAI_API_KEY`, `AWS_ROLE_TO_ASSUME`, `CODEX_REVIEW_APP_PRIVATE_KEY`, and `ACTION_REPO_TOKEN` only if you run a private copy of this repository.

### Just the trigger

To keep your own review job and only reuse the gate, use the trigger action on its own:

```yaml
- id: trigger
  uses: piyush-gambhir/codex-pr-review/trigger@v1
  with:
    base-branches: main
    label: codex-review
```

It outputs `run` (`true` or `false`), `pr-number`, `head-sha`, `base-ref` (already `origin/`-prefixed), `provider`, `effort`, `full`, `force`, `comment-id` and `reason` (why no review runs, e.g. `no-command`, `no-write-access`, `fork`, `base-branch-not-allowed`). It needs `pull-requests: write` and `issues: write` to react, reply and remove the label.

> **How the reusable workflow finds its own action.** `github.workflow_ref` and `github.workflow_sha` describe the *caller*, and `./` resolves against the caller's checkout, so neither can name the action version that belongs with the workflow. Each job instead checks out `job.workflow_repository` at `job.workflow_sha` (the workflow file defining the running job, which is this workflow) and uses the action from that checkout. So `@v1.1.0` of the workflow runs v1.1.0 of the action, a full-SHA pin runs that SHA, and a private copy of this repository uses itself. The trade-off: [`job.workflow_*`](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context) does not exist on GitHub Enterprise Server, so use the standalone example there.

## Customising

### Repository guidelines

Put review rules in `.github/codex-review.md` on your default branch (or point `guidelines-path` elsewhere). The workflow loads that file from the default branch (never from the PR, so a PR can't rewrite the rules it's reviewed against) and passes it to Codex. Codex applies it on top of its own review logic. See [`examples/codex-review.md`](examples/codex-review.md).

Guidelines are good for:

- **Severity**: "any query not scoped to the caller's tenant is P0".
- **Conventions**: "money is stored as integer cents".
- **Exclusions**: "ignore generated code under `src/gen/`".

You can also pass rules inline with `review-instructions`.

### Filtering and gating

- `max-priority: P1` reports only P0 and P1 findings; the number hidden is noted under the review.
- `fail-on-priority: P1` fails the step when a P0 or P1 finding is reported. Make the check required if it should block merging. The review is still posted.

### Re-reviews

Every posted review carries a hidden state marker with the reviewed commit, a digest of the settings that produced it, and a fingerprint per finding (the path plus the meaningful words of the title, so it survives line shifts and small rewordings). The next review reads the most recent marker on the pull request and uses it to:

- skip the review entirely when the commit and the settings are the same ones (see [Not paying twice](#not-paying-twice));
- list what is gone under **Resolved since last review** (collapsed once it gets long) and count it in the verdict line, e.g. `2 issues to address (2 P1) · 3 resolved`;
- tag findings reported again as **still open** in the issues table;
- resolve the inline threads of fixed findings, so only live feedback is left open. Turn that off with `resolve-fixed-threads: false`.

With `incremental: true` a re-review looks only at the commits pushed since the last review (`--base <previous sha>`), which is faster and cheaper on long-running PRs; the meta line then says `Incremental: abc1234..def5678`. Findings in files those commits didn't touch are not re-checked, so they are carried forward as **still open, not re-checked** rather than counted as fixed. If the previous commit is no longer in the branch's history (a force-push), the run falls back to a full review and says so.

### Not paying twice

A review costs a model call and about a minute. Three things make sure you only pay when there is something to pay for.

**The same commit is not reviewed twice.** Every posted review's state marker carries a digest of the settings that produced it: provider, model, effort, base ref, review mode, guidelines, path filters, priority cut-off and post mode. Review mode is in there, so a single review never satisfies a later `@gpt review full` on the same commit. When a request arrives for a commit that already has a review with the same digest, nothing is installed and no model is called; a short note goes up linking to the review that is already there. Ask again with `force` (`@gpt review force`) to review it anyway, or set `skip-unchanged: false` to turn it off. Anything that could change the answer stops the skip on its own, including a marker written before this feature existed. The `skipped`, `skip-reason` and `existing-review-url` outputs say what happened.

**A newer request cancels the one in flight.** The review job's concurrency group is per pull request with `cancel-in-progress: true`, so pushing a fix and asking again does not leave the first review running against the old commit. The cancelled run removes its own progress note and posts no failure note; the run that replaced it posts its own.

**The reviewer is installed once per runner, not once per run.** The Codex CLI is about 300 MB unpacked, so it is cached with `actions/cache`, keyed on the runner's OS and architecture, the Codex version and the pnpm major. With the pinned `codex-version` the key is known before anything is set up, so a warm cache skips `actions/setup-node`, `pnpm/action-setup` and the install itself: measured on `ubuntu-24.04`, the install path goes from about 14 s to about 2 s. With `codex-version: latest` the version is resolved first (pnpm's own resolution, because its minimum release age means `latest` is usually not the newest release), so the key always names the version that gets installed; if it cannot be resolved the cache sits that run out rather than risk restoring the wrong version. A restored install is only used when it runs and reports the version asked for. Install scripts stay disabled either way. `cache-install: false` turns it off, for runners with no cache service.

### Checkout cost

The review diffs against the merge base with the base branch, so the PR is checked out with `fetch-depth: 0` and `persist-credentials: false`. Those two together are the reason the checkout is not cheaper:

- A `filter: blob:none` partial clone is much cheaper to fetch (on a repository with a few thousand commits: 3.6 s and 4.4 MB against 9.0 s and 81 MB) but it fetches file contents lazily from the remote. With no credential left in the checkout, `git diff` between the merge base and HEAD fails outright on a private repository (`could not fetch <oid> from promisor remote`), which breaks both the size guard and the review. Keeping a credential there would put a token in the checkout Codex reads, which is exactly what `persist-credentials: false` is for. Not adopted.
- A shallow checkout plus a targeted `git fetch --deepen` for the base branch needs a credential for that fetch, for the same reason. Not adopted.

So `fetch-depth: 0` stays. On a very large repository, the honest options are a runner with a warm local mirror, or `max-changed-lines` with `large-pr: skip` so oversized pull requests cost one `git diff` and nothing else.

Two more things worth setting in your own workflow: `timeout-minutes` on the review job (the example uses 30; the model call is the only slow step and it is billed for as long as it runs), and nothing on `push` or `pull_request` triggers, so a review only happens when someone asks.

### Paths

`include-paths` and `exclude-paths` take glob patterns, one per line or comma separated:

```yaml
include-paths: |
  src/**/*.ts
  api/**
exclude-paths: |
  **/gen/**
  *.lock
```

- `*`, `?` and `[abc]` stay inside one path segment; `**` spans whole segments, and `src/**/*.ts` also matches `src/a.ts`.
- A pattern with no `/` matches the file name anywhere in the tree (`*.lock` covers `web/yarn.lock`); with a `/` it is anchored at the repository root.
- Exclusions win over inclusions. Findings outside the filter are dropped after parsing, counted in `path-filtered-count` and noted under the review, and `exclude-paths` is passed to Codex as well so it doesn't spend effort there.

### Suggested fixes

`suggestions: true` (the default) asks Codex to attach the full replacement for the lines it flagged as a fenced `suggestion` block. Findings that carry one are marked with the suggestion icon in the issues table.

A block only becomes a real GitHub suggestion when the inline comment covers exactly the lines the fix replaces; otherwise (a range GitHub won't anchor, a finding shown in the details, or `post-mode: comment`) it is rendered as a plain "Suggested fix" code block. Set `suggestions: false` to stop asking for them, and to render any that turn up anyway as plain blocks.

### Large pull requests

`max-changed-lines` sets a size above which a pull request is treated as too big. The count is added plus deleted lines between the merge base of `base-ref` and HEAD, after the path filters, and is always available as the `changed-lines` output.

- `large-pr: warn` (the default) reviews anyway and adds a note under the review.
- `large-pr: skip` posts a short "PR too large to review" note and finishes successfully without calling Codex, so nothing is spent on it.
- `large-pr: full` reviews it shard by shard instead, which is the same thing as `review-mode: auto` (see [Full reviews](#full-reviews)).

```yaml
max-changed-lines: 3000
large-pr: skip
```

### Full reviews

One `codex exec review` pass samples. On a 48,773-line pull request (406 files) it made 29 model requests in about four minutes and reported six findings, all in two areas: Codex reads what looks risky and stops. That is the right trade for an ordinary PR and the wrong one for a big rewrite, where the question is not "what stands out" but "was any of this read at all".

`review-mode: full` answers that question. Ask for it per run (`@gpt review full`), always (`review-mode: full`), or only over a size you choose (`review-mode: auto` with `max-changed-lines`).

```yaml
review-mode: auto
max-changed-lines: 3000
shard-lines: 2500      # changed lines per shard
max-parallel: 4        # shard passes at once
max-shards: 24         # a bigger PR gets bigger shards, not more passes
```

**How a shard keeps the whole pull request.** The diff is cut into shards of about `shard-lines` changed lines, keeping directories together, never splitting a file, and packing tests after the source they belong with. Each shard is then reviewed in a checkout of the **full** pull request head, against a synthetic base commit that is HEAD's tree with only that shard's files put back to their merge-base versions. `git diff <that commit> HEAD` is exactly the shard, while every other file is present at its final state, so Codex reads the real callers and the real call sites rather than a slice of a diff. The commits are built with Git plumbing against a temporary index: no ref is created, and neither your working tree nor the repository's index is touched.

Shard passes run concurrently, each with its own `CODEX_HOME` so their session rollouts never collide, and each bounded by `pass-timeout-minutes` inside an overall `review-budget-minutes`. Then two more passes: any changed file no pass actually read gets one follow-up pass of its own, and a final **cross-cutting** pass reviews the whole diff with the per-area summaries in its instructions, asked only for what a single area cannot show (an API change against its callers, wiring, auth applied unevenly, a migration against the code that reads those columns). Every pass's findings are merged and de-duplicated on the same fingerprint re-reviews use, keeping the worst priority and any suggested fix, and posted as one review.

**What it costs.** Roughly one model call per shard plus two, and the passes overlap, so the wall clock is about `shards / max-parallel` times a single review rather than `shards` times it. The review's meta line and the `input-tokens`, `output-tokens` and `estimated-cost-usd` outputs cover every pass together. Give the job room: `timeout-minutes` should be comfortably above `review-budget-minutes` plus the install.

**What coverage means.** Every run, in both modes, writes `$RUNNER_TEMP/codex-review-coverage.json`:

```json
{"mode": "full", "complete": true, "files_total": 224, "files_inspected": 224,
 "uncovered": [], "shards": 9, "passes": 12}
```

and shows it in the meta line under the review (`Coverage 224/224 files (full, 12 passes)`), with `coverage-files-total`, `coverage-files-inspected` and `coverage-complete` as outputs. It is measured, not assumed: [`scripts/coverage.py`](scripts/coverage.py) reads the session rollouts Codex wrote and counts a changed file as inspected when Codex read the file itself (`cat`, `sed -n`, `nl`, a file-reading tool call) or read a diff whose output contained it. Listings and searches (`ls`, `rg`, `git diff --stat`) do not count, and a diff that was truncated before it reached a file counts only as far as it got. A pass that times out or fails leaves its files in `uncovered` rather than disappearing.

**The honest caveat.** "Inspected" means the reviewer had the file in front of it, not that every bug in it was found. `complete: true` rules out one failure mode, the file nobody looked at; it does not promise the review is right. Treat it as a floor under the review's scope, not a guarantee about its findings.

### Output

- `post-mode: review` (default): verdict and issues table in the review body, plus inline comments.
- `post-mode: comment`: one comment with the table and collapsible details.
- `post-mode: none`: nothing posted; use the outputs and the job summary.

Every run also writes the review to the workflow's job summary.

### Icons

The review, the status notes, the check run summary and the job summary all use the same small set of 16px [Octicons](icons/) (MIT, recoloured for what each one means) instead of emoji. GitHub proxies images in comments through camo, so they are referenced by URL rather than inlined, and every one carries `alt` text and an explicit 16x16 size so a review still reads correctly if an image never loads.

| Input | Default | Description |
|---|---|---|
| `icons` | `true` | `false` renders every layout in words, with no external images at all |
| `icon-base-url` | jsDelivr | Base URL to load the images from, without a trailing slash |

```yaml
icons: false # GitHub Enterprise Server, or an organisation that blocks camo
```

The default URL is jsDelivr for this repository's `icons/` directory:

```
https://cdn.jsdelivr.net/gh/piyush-gambhir/codex-pr-review@<ref>/icons/<file>.svg
```

`<ref>` is the ref this action was resolved from, but only when that really is the public `piyush-gambhir/codex-pr-review` at a tag (`v1`, `v1.3.0`) or a full commit SHA. A local `./` checkout, a private copy of this repository or a branch ref instead gets the pinned fallback ref in [`scripts/icons.py`](scripts/icons.py) (`PINNED_REF`), so an image URL can never point at a private repository or at a branch that moves underneath a posted review. That constant is bumped as part of cutting a release whenever `icons/` changes; see the release note in [CHANGELOG.md](CHANGELOG.md).

Point `icon-base-url` at your own host to serve them from somewhere a locked-down network can reach, keeping the file names in [`icons/README.md`](icons/README.md). That file also documents the colours, their measured contrast against GitHub's light and dark backgrounds, and how to regenerate the set with [`scripts/dev/build_icons.py`](scripts/dev/build_icons.py).

### Checks and code scanning

Two optional outputs put the findings where GitHub already shows problems: a check run (annotations on the diff, a conclusion the branch protection rules can require) and a SARIF file (code scanning alerts, with history and dismissals).

```yaml
    permissions:
      contents: read
      pull-requests: write
      checks: write # check-run: true
      security-events: write # upload-sarif
    steps:
      # ...checkout steps...
      - id: codex
        uses: piyush-gambhir/codex-pr-review@v1
        with:
          # ...the usual inputs...
          check-run: true
          sarif-file: codex-review.sarif

      - name: Upload to code scanning
        if: always() && steps.codex.outputs.sarif-file != ''
        uses: github/codeql-action/upload-sarif@v4
        with:
          sarif_file: ${{ steps.codex.outputs.sarif-file }}
          checkout_path: ${{ github.workspace }}/pr # where the PR is checked out
          category: codex-review
```

**`check-run: true`** creates a check run named after `title` on the reviewed commit. It appears as "in progress" while Codex reviews, then completes with:

| Conclusion | When |
|---|---|
| `success` | no findings |
| `neutral` | findings, but none at or above `fail-on-priority` (or gating is off) |
| `failure` | `fail-on-priority` tripped, or the review itself failed |
| `cancelled` | the job was cancelled |

The output holds the verdict and the issues table, plus one annotation per finding: `failure` for P0 and P1, `warning` for P2, `notice` for P3. Annotations go up 50 at a time, as the API requires.

Needs `checks: write`. Without it the API answers 403, the action warns and the review is posted as usual. Note that check runs created with `GITHUB_TOKEN` (or with the action's own app token) attach to the workflow's **own check suite**, so the run is listed next to the review job rather than under a suite of its own; that is a GitHub restriction, not a setting.

**`sarif-file`** writes the findings as SARIF 2.1.0, with one rule per priority (`codex-review/p0` to `codex-review/p3`), a level per priority (`error`, `error`, `warning`, `note`) and fingerprints over path and title, so a re-review updates alerts instead of duplicating them. The file is written even when `fail-on-priority` fails the step, so the upload step needs `if: always()`. Findings paths are relative to `working-directory`, so pass that directory as `checkout_path` when the PR is not checked out at the workspace root. Code scanning keeps alerts for files in the analysed commit only.

### Your own bot name and avatar

By default reviews are posted by `github-actions`. To post under your own name and icon, use a GitHub App:

1. Create a GitHub App (Settings, Developer settings, GitHub Apps, New). Name it what you want the bot to be called, for example `Acme Code Review`. Disable the webhook. Repository permissions: **Pull requests: read and write**, **Issues: read and write**.
2. Upload the logo you want the bot to show (App settings, Display information).
3. Install the app on your repository, and generate a private key.
4. Add the repository variable `CODEX_REVIEW_APP_CLIENT_ID` (the app's **Client ID**, shown on its settings page) and the secret `CODEX_REVIEW_APP_PRIVATE_KEY` (the `.pem` contents).
5. Pass the client ID to the reusable workflow: `with: { app-client-id: "${{ vars.CODEX_REVIEW_APP_CLIENT_ID }}" }`.

The workflow mints an app token with `actions/create-github-app-token` and passes it as `github-token`. Reviews then appear as `your-app-name[bot]` with the app's logo. If you use the Codex name or logo for the app, check OpenAI's brand guidelines first.

## Amazon Bedrock

1. Enable the OpenAI model in Amazon Bedrock (for example GPT-6 Sol in `us-east-1`).
2. Add GitHub as an IAM OIDC identity provider: `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
3. Create an IAM role trusted only for `repo:OWNER/REPO:ref:refs/heads/<default branch>` (comment-triggered workflows always run from the default branch), with:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"],
         "Resource": [
           "arn:aws:bedrock:us-east-1:ACCOUNT_ID:inference-profile/us.openai.gpt-6-sol",
           "arn:aws:bedrock:*::foundation-model/openai.gpt-6-sol*"
         ]
       },
       { "Effect": "Allow", "Action": "bedrock:ListInferenceProfiles", "Resource": "*" }
     ]
   }
   ```

4. Add the role ARN as the Actions secret `AWS_ROLE_TO_ASSUME`, then comment `@gpt review bedrock`.

## Inputs

| Input | Default | Description |
|---|---|---|
| **Model** | | |
| `provider` | `openai` | `openai` or `bedrock` |
| `model` | per provider | `gpt-6.1-sol` (openai) or `us.openai.gpt-6-sol` (bedrock) |
| `reasoning-effort` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `openai-api-key` | | Required for `openai` |
| `aws-role-to-assume` | | Required for `bedrock` |
| `aws-region` | `us-east-1` | Bedrock region |
| `bedrock-endpoint` | `amazon-bedrock-runtime` | `amazon-bedrock` for Mantle (in-region IDs such as `openai.gpt-6-sol`; needs `bedrock-mantle:CreateInference`) |
| **What to review** | | |
| `base-ref` | required | Ref to diff against, e.g. `origin/main`; check out with `fetch-depth: 0` |
| `pr-number` | required | Pull request to post on |
| `head-sha` | required | Commit the review is anchored to |
| `working-directory` | `.` | Where the PR is checked out |
| `include-paths` | | Only report findings in files matching these globs (newline or comma separated) |
| `exclude-paths` | | Never report findings in files matching these globs; Codex is told to skip them too |
| `max-changed-lines` | | Added plus deleted lines above which `large-pr` applies; empty means no limit |
| `large-pr` | `warn` | Over the limit: `warn` (review anyway), `skip` (post a note, skip the review) or `full` (review it shard by shard) |
| `review-mode` | `single` | `single`, `full` (every changed file, shard by shard) or `auto` (full over `max-changed-lines`); see [Full reviews](#full-reviews) |
| `shard-lines` | `2500` | Changed lines per shard in a full review |
| `max-shards` | `24` | Most shards a full review may plan; a bigger PR gets bigger shards, not more passes |
| `max-parallel` | `4` | Shard passes to run at once, each with its own `CODEX_HOME` |
| `pass-timeout-minutes` | `10` | How long one pass may take; a pass that runs out is reported as uncovered |
| `review-budget-minutes` | `20` | How long all the passes may take together; keep the job's `timeout-minutes` above it |
| **Review behaviour** | | |
| `suggestions` | `true` | Ask Codex for committable fixes as GitHub suggestions |
| `review-instructions` | | Inline review guidelines |
| `review-instructions-file` | | Guidelines file, relative to the workspace |
| `max-priority` | `P3` | Lowest priority to report |
| `fail-on-priority` | | Fail the step when a finding at this priority or higher is reported |
| `resolve-fixed-threads` | `true` | Resolve the inline threads of findings no longer reported |
| `incremental` | `false` | Review only the commits pushed since the last Codex review |
| `skip-unchanged` | `true` | Skip the review when this commit already has one with the same settings |
| `force` | `false` | Review anyway, whatever `skip-unchanged` would have decided |
| **Output** | | |
| `post-mode` | `review` | `review`, `comment` or `none` |
| `hide-previous` | `true` | Collapse earlier Codex comments as outdated |
| `status-comment` | `true` | Progress note while reviewing; becomes a failure note on errors |
| `trigger-comment-id` | | Requesting comment; gets a rocket reaction on success and a confused one on failure |
| `rerun-hint` | | Next-steps line under the review |
| `title` | `Codex review` | Review heading |
| `icons` | `true` | Show the Octicon images; `false` renders every layout as text only |
| `icon-base-url` | jsDelivr | Base URL the icon images are loaded from, without a trailing slash |
| `check-run` | `false` | Create a check run with the verdict and one annotation per finding; needs `checks: write` |
| `sarif-file` | | Also write the findings as SARIF 2.1.0 to this path, for code scanning |
| `github-token` | `github.token` | Needs `pull-requests: write`; use an app token for a custom bot identity |
| **Advanced** | | |
| `pricing` | built-in table | JSON override, per 1M tokens: `{"my-model": [input, cached-input, output]}` |
| `sandbox` | `read-only` | Codex sandbox for commands it runs while reviewing |
| `codex-config` | | Extra raw TOML for Codex's `config.toml` |
| `codex-version` | `0.159.2` | Codex CLI version to install: a pinned version for reproducible reviews, or `latest` (the newest release older than pnpm's minimum release age, a supply-chain safety delay) |
| `node-version` | `lts/*` | Node.js set up with `actions/setup-node` for installing and running Codex; empty keeps the runner's Node, and an open request such as `lts/*` is skipped when the runner's Node already satisfies it |
| `pnpm-version` | `12` | pnpm set up with `pnpm/action-setup` to install Codex; skipped when the CLI is already there |
| `cache-install` | `true` | Cache the installed Codex CLI with `actions/cache` (see [Not paying twice](#not-paying-twice)) |

## Outputs

| Output | Description |
|---|---|
| `findings-count` | Findings reported (after `max-priority`) |
| `highest-priority` | e.g. `P1`; empty when clean |
| `filtered-count` | Findings hidden by `max-priority` |
| `resolved-count` | Previous findings no longer reported (fixed since the last review) |
| `path-filtered-count` | Findings hidden by `include-paths` or `exclude-paths` |
| `changed-lines` | Added plus deleted lines between the merge base and HEAD, after the path filters |
| `review-mode` | What this run did: `single` or `full` |
| `coverage-files-total` | Changed files the review was meant to cover, after the path filters |
| `coverage-files-inspected` | Changed files Codex actually read, from the session rollouts |
| `coverage-complete` | `true` when every changed file was inspected |
| `findings-file` | JSON file: `priority`, `title`, `path`, `start`, `end`, `body`, `fingerprint`, `suggestion` per finding |
| `review-file` | Codex's raw review message |
| `input-tokens` | Input tokens used, cached ones included; empty when Codex reported no usage |
| `cached-input-tokens` | Input tokens served from the prompt cache |
| `output-tokens` | Output tokens used, reasoning tokens included |
| `estimated-cost-usd` | Estimated cost in US dollars; empty for a model with no known price |
| `sarif-file` | The SARIF file, when `sarif-file` was set |
| `check-run-id` | The check run, when `check-run` is enabled |
| `skipped` | `true` when no review ran (too large, or already reviewed) |
| `skip-reason` | `large-pr`, `already-reviewed`, or empty when a review ran |
| `existing-review-url` | The review a skipped run pointed at |

## Models and prices

| Model | Provider | Per 1M tokens (input / cached / output) |
|---|---|---|
| `gpt-6.1-sol` | openai | $2.00 / $0.10 / $10.00 |
| `gpt-6-sol`, `us.openai.gpt-6-sol` | openai, bedrock | $2.00 / $0.20 / $10.00 |
| `gpt-6-astra`, `us.openai.gpt-6-astra` | openai, bedrock | $10.00 / $1.00 / $50.00 |
| `gpt-6-luna`, `us.openai.gpt-6-luna` | openai, bedrock | $0.10 / $0.01 / $0.50 |
| `gpt-5.6-sol` | openai | $4.00 / $0.40 / $20.00 |
| `gpt-5.6-terra` | openai | $2.00 / $0.20 / $12.00 |
| `gpt-5.6-luna` | openai | $0.20 / $0.02 / $1.20 |
| `gpt-5.5` | openai | $5.00 / $0.50 / $30.00 |

Standard OpenAI API rates, which Bedrock matches; Bedrock IDs (`us.openai.gpt-6-sol`, `openai.gpt-6-sol`) are priced as the model they name.

Every review shows what it used and what it cost, for example `36,615 input (29,312 cached) + 926 output tokens · ~$0.03`. Reasoning tokens are billed as output. Requests with more than 272K input tokens are priced at the long-context rate (2x input, 1.5x output). It is an estimate from the table above, not a bill: check OpenAI or AWS for what you were actually charged. A model that isn't in the table shows tokens without a cost, unless you price it with the `pricing` input:

```yaml
pricing: '{"my-fine-tune": [2, 0.2, 10]}'
```

## How it works

1. **Size guard and review mode**: [`scripts/filters.py`](scripts/filters.py) counts the changed lines between the merge base and HEAD. Over `max-changed-lines` with `large-pr: skip`, a note goes up and the remaining steps are skipped. The same count resolves `review-mode` to `single` or `full`, which the settings digest below then carries.
2. **Previous review**: [`scripts/history.py`](scripts/history.py) reads the state marker in the last Codex review on the PR, and with `incremental` checks whether that commit is still an ancestor of the head. It runs before anything is posted, so [`scripts/pr_state.py`](scripts/pr_state.py) can read the whole conversation in one GraphQL call and hand the later steps what they need (the comments to collapse, the threads to resolve) instead of listing the pull request again.
3. **Already reviewed?** [`scripts/rereview.py`](scripts/rereview.py) compares the marker's commit and settings digest with this run's. A match posts a note linking to that review and skips everything below. Nothing has been installed or posted at this point.
4. **Progress note**: posted on the PR with a link to the run.
5. **Install**: [`scripts/install_plan.py`](scripts/install_plan.py) works out the cache key, the CLI is restored with `actions/cache`, and Node.js, pnpm and the install itself only happen when the restored CLI is not usable. Installs go into `RUNNER_TEMP` with install scripts disabled.
6. **Credentials**: for `bedrock`, `aws-actions/configure-aws-credentials` assumes the role via OIDC and returns credentials as step outputs. For `openai`, the key is passed as `CODEX_API_KEY`. Only the review step receives them, and no GitHub token reaches Codex.
7. **Config**: [`scripts/write_config.py`](scripts/write_config.py) writes Codex's `config.toml`: provider, model, effort, a read-only sandbox, no approvals, your guidelines, and a minimal environment (`shell_environment_policy.inherit = "core"`) so commands Codex runs never see the credentials.
8. **Review**: `codex exec review --base <base-ref>` reviews the diff against the merge base. In `full` mode [`scripts/full_review.py`](scripts/full_review.py) runs that once per shard instead: [`scripts/shards.py`](scripts/shards.py) plans the shards and builds each one's synthetic base with Git plumbing, the passes run concurrently in reused worktrees of the pull request head, and the findings of every pass are merged back into one review message in Codex's own layout.
9. **Usage**: [`scripts/usage.py`](scripts/usage.py) reads the real token counts out of the session rollouts Codex wrote (review mode reports zero usage on its `turn.completed` event) and estimates the cost from the price table. A full review lists its per-pass homes in `CODEX_HOMES_FILE`, so the cost covers all of them.
10. **Coverage**: [`scripts/coverage.py`](scripts/coverage.py) reads the same rollouts for the commands Codex ran and works out which changed files it actually looked at, writing `$RUNNER_TEMP/codex-review-coverage.json` in both modes.
11. **Publish**: [`scripts/publish_review.py`](scripts/publish_review.py) parses the findings and posts the review. If GitHub rejects an inline anchor, everything goes in the body instead. [`scripts/status.py`](scripts/status.py) then clears the progress note, or turns it into a failure note. A failed or empty review never looks like a pass. Fixed findings are listed as resolved and their threads are resolved, best effort.
12. **Checks and SARIF** (optional): [`scripts/checks.py`](scripts/checks.py) completes the check run with the verdict and the annotations, and [`scripts/sarif.py`](scripts/sarif.py) turns the findings JSON into a SARIF 2.1.0 file. Neither can fail the review.

## Security

- **Trusted contributors only.** The review job holds model credentials and checks out PR code. It never builds or runs that code, but Codex may run read-only commands in its sandbox while reviewing. The trigger skips fork PRs and requires write access.
- **Don't use `pull_request_target`** with this action.
- **Load guidelines from the default branch**, as the workflow does.
- **Pin the action** to a tag or commit SHA.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests cover:

- parsing real Codex output, including layouts nudged by custom guidelines
- the trigger: option parsing, every event, access, forks, base branches, label removal
- filtering, gating and rendering
- suggestion blocks, path globs and the large-PR guard
- status notes
- config generation
- token usage and cost, against a captured Codex session rollout
- check run conclusions, annotation batching and summary truncation
- SARIF 2.1.0 structure
- fingerprints, state round trips, resolved versus still-open classification (full and incremental) and thread matching
- the icon set: URL resolution (an action tag, a commit SHA, a branch, a private copy), alert type selection, alt text and sizing, text-only mode, and that nothing the action renders or this repository ships contains an emoji codepoint
- the settings digest, what does and does not change it, and the skip decision for every case
- the install plan: the runner probes, exact versus floating versions, lockfile parsing and cache keys
- how many times a run reads the pull request, with and without the shared read
- shard planning (directories kept together, files never split, tests apart, a deterministic order, the shard budget) and the synthetic bases, against real temporary repositories: added, deleted, renamed and binary files, that `git diff` between the two commits is the shard and nothing else, and that building them touches no ref, index or working tree
- coverage extraction from a captured session rollout: what counts as reading a file, what a summary diff and a search do not count as, where a truncated diff stops, and the coverage file in both modes
- a whole full review against a temporary repository with a stand-in reviewer: two shards, per-pass homes, the cross-cutting pass, the merge, summed usage, and worktrees left clean
- merging and de-duplicating findings across passes, and that the merged message parses back as a Codex review
- a pass that times out, one that fails and one that never starts

The end-to-end harness runs the real thing against a real pull request with your local Codex login:

```bash
GH_TOKEN=$(gh auth token) scripts/dev/e2e_local.sh <owner/repo> <pr>
FULL=1 SHARD_LINES=10 GH_TOKEN=$(gh auth token) scripts/dev/e2e_local.sh <owner/repo> <pr>
```

`DRY_RUN=1` posts nothing. A tiny `SHARD_LINES` forces real sharding on a small pull request.

## License

[MIT](LICENSE)

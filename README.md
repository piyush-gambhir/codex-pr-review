<p align="center"><img src="assets/icon.png" width="96" alt="Codex PR Review icon"></p>

<h1 align="center">Codex PR Review</h1>

<p align="center">On-demand pull request reviews from <b>Codex's native reviewer</b>, on OpenAI models via the <b>OpenAI API</b> or <b>Amazon Bedrock</b>.</p>

Comment `@gpt review` on a pull request. About a minute later you get a review with a verdict, an issues table linking every finding to its exact lines, and inline comments on the diff. The review uses `codex exec review`, the same logic as `/review` in the Codex CLI, so you get OpenAI's own review behaviour rather than a hand-written prompt.

| `provider` | Auth | Billing | Default model |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` secret | OpenAI API | `gpt-6.1-sol` |
| `bedrock` | IAM role via GitHub OIDC, no stored keys | Your AWS account, at OpenAI's rates | `us.openai.gpt-6-sol` |

> This is an independent project, not an official OpenAI or AWS product.

## What a review looks like

> ## Codex review
>
> 🟠 **5 issues to address** (4 P1, 1 P2)
>
> The patch introduces five correctness issues, including incorrect discounts and non-integer monetary results.
>
> | | Priority | Issue | Location | |
> |---|---|---|---|---|
> | 🟠 | P1 | Convert percentage points to a fraction | `e2e/pricing.ts:16` | 💬 inline |
> | 🟠 | P1 | Round discounted totals to integer cents | `e2e/pricing.ts:16-17` | 💬 inline |
> | 🟠 | P1 | Handle zero units before calculating the average | `e2e/pricing.ts:22` | 💬 inline |
> | 🟠 | P1 | Round average unit prices to integer cents | `e2e/pricing.ts:22` | 💬 inline |
> | 🟡 | P2 | Sort ascending to select the cheapest item | `e2e/pricing.ts:26` | 💬 inline |
>
> <sub>Reviewed `314c52a` against `main` · `gpt-6.1-sol` via OpenAI API · medium effort · View run</sub><br>
> <sub>Re-run: `@gpt review` · deeper: `@gpt review high` · on Bedrock: `@gpt review bedrock`</sub>

- **Verdict first**: ✅ no issues, 🟡 minor issues, or 🟠/🔴 issues to address, with counts per priority.
- **Issues table**: every finding with its priority (P0 to P3) and a `file:lines` link pinned to the reviewed commit.
- **Inline comments** on the exact diff lines (including multi-line ranges); findings outside the diff appear as collapsible details, so nothing is lost.
- **Progress and failures on the PR**: a "🔍 in progress" note (with a link to the live run) is replaced by the review. If anything fails, it becomes "❌ failed" with the actual error. The requesting comment gets 👀, then 🚀 or 😕.
- **Re-reviews stay tidy**: earlier Codex comments are collapsed as outdated, fixed findings are listed as resolved and their inline threads are resolved on GitHub.
- **Next steps** under every review: how to re-run, go deeper, or switch provider.

## Quick start

1. Add an `OPENAI_API_KEY` Actions secret (or set up [Bedrock](#amazon-bedrock)).
2. Copy [`examples/codex-review.yml`](examples/codex-review.yml) to `.github/workflows/codex-review.yml`, set `BASE_BRANCH`, and merge it into your default branch (comment-triggered workflows run from there).
3. Comment on an open pull request:

   | Comment | Does |
   |---|---|
   | `@gpt review` | Review with the defaults |
   | `@gpt review high` | Deeper reasoning (`low`, `medium`, `high`, `xhigh`) |
   | `@gpt review bedrock` | Use Amazon Bedrock for this request |
   | `@gpt review bedrock xhigh` | Both |

   Defaults come from the repository variables `CODEX_REVIEW_PROVIDER` (`openai`) and `CODEX_REVIEW_EFFORT` (`medium`).

The example workflow only reviews open, same-repository PRs into `BASE_BRANCH`, requested by someone with write access, one review at a time per PR. Nothing runs on open or push, so you only pay for reviews someone asks for.

## Customising

### Repository guidelines

Put review rules in `.github/codex-review.md` on your default branch. The example workflow loads that file from the default branch (never from the PR, so a PR can't rewrite the rules it's reviewed against) and passes it to Codex. Codex applies it on top of its own review logic. See [`examples/codex-review.md`](examples/codex-review.md).

Guidelines are good for:

- **Severity**: "any query not scoped to the caller's tenant is P0".
- **Conventions**: "money is stored as integer cents".
- **Exclusions**: "ignore generated code under `src/gen/`".

You can also pass rules inline with `review-instructions`.

### Filtering and gating

- `max-priority: P1` reports only P0 and P1 findings; the number hidden is noted under the review.
- `fail-on-priority: P1` fails the step when a P0 or P1 finding is reported. Make the check required if it should block merging. The review is still posted.

### Re-reviews

Every posted review carries a hidden state marker with the reviewed commit and a fingerprint per finding (the path plus the meaningful words of the title, so it survives line shifts and small rewordings). The next review reads the most recent marker on the pull request and uses it to:

- list what is gone under **Resolved since last review** (collapsed once it gets long) and count it in the verdict line, e.g. `2 issues to address (2 P1) · 3 resolved`;
- tag findings reported again as **still open** in the issues table;
- resolve the inline threads of fixed findings, so only live feedback is left open. Turn that off with `resolve-fixed-threads: false`.

With `incremental: true` a re-review looks only at the commits pushed since the last review (`--base <previous sha>`), which is faster and cheaper on long-running PRs; the meta line then says `Incremental: abc1234..def5678`. Findings in files those commits didn't touch are not re-checked, so they are carried forward as **still open, not re-checked** rather than counted as fixed. If the previous commit is no longer in the branch's history (a force-push), the run falls back to a full review and says so.

### Output

- `post-mode: review` (default): verdict and issues table in the review body, plus inline comments.
- `post-mode: comment`: one comment with the table and collapsible details.
- `post-mode: none`: nothing posted; use the outputs and the job summary.

Every run also writes the review to the workflow's job summary.

### Your own bot name and avatar

By default reviews are posted by `github-actions`. To post under your own name and icon, use a GitHub App:

1. Create a GitHub App (Settings → Developer settings → GitHub Apps → New). Name it what you want the bot to be called, for example `Acme Code Review`. Disable the webhook. Repository permissions: **Pull requests: read and write**, **Issues: read and write**.
2. Upload a logo. [`assets/icon.png`](assets/icon.png) is free to use, or bring your own.
3. Install the app on your repository, and generate a private key.
4. Add the repository variable `CODEX_REVIEW_APP_ID` (the app ID) and the secret `CODEX_REVIEW_APP_PRIVATE_KEY` (the `.pem` contents).

The example workflow detects the variable, mints an app token with `actions/create-github-app-token`, and passes it as `github-token`. Reviews then appear as `your-app-name[bot]` with your logo. Please don't use OpenAI's or Codex's name or logo for your app, so readers don't mistake it for an official product.

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
| **Review behaviour** | | |
| `review-instructions` | | Inline review guidelines |
| `review-instructions-file` | | Guidelines file, relative to the workspace |
| `max-priority` | `P3` | Lowest priority to report |
| `fail-on-priority` | | Fail the step when a finding at this priority or higher is reported |
| `resolve-fixed-threads` | `true` | Resolve the inline threads of findings no longer reported |
| `incremental` | `false` | Review only the commits pushed since the last Codex review |
| **Output** | | |
| `post-mode` | `review` | `review`, `comment` or `none` |
| `hide-previous` | `true` | Collapse earlier Codex comments as outdated |
| `status-comment` | `true` | Progress note while reviewing; becomes a failure note on errors |
| `trigger-comment-id` | | Requesting comment; gets 🚀 on success and 😕 on failure |
| `rerun-hint` | | Next-steps line under the review |
| `title` | `Codex review` | Review heading |
| `github-token` | `github.token` | Needs `pull-requests: write`; use an app token for a custom bot identity |
| **Advanced** | | |
| `sandbox` | `read-only` | Codex sandbox for commands it runs while reviewing |
| `codex-config` | | Extra raw TOML for Codex's `config.toml` |
| `codex-version` | `0.159.1` | Pinned Codex CLI version |

## Outputs

| Output | Description |
|---|---|
| `findings-count` | Findings reported (after `max-priority`) |
| `highest-priority` | e.g. `P1`; empty when clean |
| `filtered-count` | Findings hidden by `max-priority` |
| `resolved-count` | Previous findings no longer reported (fixed since the last review) |
| `findings-file` | JSON file: `priority`, `title`, `path`, `start`, `end`, `body`, `fingerprint` per finding |
| `review-file` | Codex's raw review message |

## Models and prices

| Model | Provider | Per 1M tokens (input / cached / output) |
|---|---|---|
| `gpt-6.1-sol` | openai | $2.00 / $0.10 / $10.00 |
| `gpt-6-sol`, `us.openai.gpt-6-sol` | openai, bedrock | $2.00 / $0.20 / $10.00 |
| `gpt-6-astra`, `us.openai.gpt-6-astra` | openai, bedrock | $10.00 / $1.00 / $50.00 |
| `gpt-6-luna`, `us.openai.gpt-6-luna` | openai, bedrock | $0.10 / $0.01 / $0.50 |

Standard OpenAI API rates, which Bedrock matches. Input above 272K tokens is billed at the long-context rate. Codex's review mode doesn't currently report token usage, so check OpenAI or AWS billing for actual cost.

## How it works

1. **Progress note**: posted on the PR with a link to the run.
2. **Previous review**: [`scripts/history.py`](scripts/history.py) reads the state marker in the last Codex review on the PR, and with `incremental` checks whether that commit is still an ancestor of the head.
3. **Install**: the pinned Codex CLI goes into `RUNNER_TEMP` with npm install scripts disabled.
4. **Credentials**: for `bedrock`, `aws-actions/configure-aws-credentials` assumes the role via OIDC and returns credentials as step outputs. For `openai`, the key is passed as `CODEX_API_KEY`. Only the review step receives them, and no GitHub token reaches Codex.
5. **Config**: [`scripts/write_config.py`](scripts/write_config.py) writes Codex's `config.toml`: provider, model, effort, a read-only sandbox, no approvals, your guidelines, and a minimal environment (`shell_environment_policy.inherit = "core"`) so commands Codex runs never see the credentials.
6. **Review**: `codex exec review --base <base-ref>` reviews the diff against the merge base.
7. **Publish**: [`scripts/publish_review.py`](scripts/publish_review.py) parses the findings and posts the review. If GitHub rejects an inline anchor, everything goes in the body instead. [`scripts/status.py`](scripts/status.py) then clears the progress note, or turns it into a failure note. A failed or empty review never looks like a pass. Fixed findings are listed as resolved and their threads are resolved, best effort.

## Security

- **Trusted contributors only.** The review job holds model credentials and checks out PR code. It never builds or runs that code, but Codex may run read-only commands in its sandbox while reviewing. The example workflow skips fork PRs and requires write access to trigger.
- **Don't use `pull_request_target`** with this action.
- **Load guidelines from the default branch**, as the example does.
- **Pin the action** to a tag or commit SHA.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests cover:

- parsing real Codex output, including layouts nudged by custom guidelines
- filtering, gating and rendering
- status notes
- config generation
- fingerprints, state round trips, resolved versus still-open classification (full and incremental) and thread matching

## License

[MIT](LICENSE)

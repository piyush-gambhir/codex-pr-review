# codex-bedrock-review

On-demand pull request review with **Codex's native reviewer** (`codex exec review`) running **OpenAI models on Amazon Bedrock**. No OpenAI API key and no stored AWS keys: the job assumes an IAM role through GitHub OIDC, and inference is billed to your AWS account at OpenAI's first-party rates.

Findings are posted as one pull request review: inline comments on lines in the diff, the rest listed in the review body, each tagged with Codex's priority (P0 to P3). A clean review posts a single comment.

## How it works

1. **Gate job** (in your workflow): a writer comments `@gpt review` on an open, same-repo PR into the base branch. Anything else is ignored.
2. **Install**: the Codex CLI (pinned) is installed into `RUNNER_TEMP` with install scripts disabled.
3. **Credentials**: `aws-actions/configure-aws-credentials` assumes the role via OIDC and returns the credentials as step outputs. Only the review step receives them, and no GitHub token is passed to Codex.
4. **Review**: `codex exec review --base <base-ref>` runs in a read-only sandbox with a config that points Codex at Bedrock:

   ```toml
   model_provider = "amazon-bedrock-runtime"
   model = "us.openai.gpt-6-sol"
   model_reasoning_effort = "medium"
   sandbox_mode = "read-only"
   approval_policy = "never"

   [shell_environment_policy]
   inherit = "core"   # commands Codex runs never see the AWS credentials
   ```

5. **Post**: `scripts/post_review.py` parses Codex's review message and creates the PR review with the job's `GITHUB_TOKEN`. If GitHub rejects an inline anchor, every finding goes in the review body instead. The step fails if Codex errors or returns nothing, so a broken review never looks like a pass.

## Setup

**AWS** (once per account):

1. Enable the OpenAI model in Amazon Bedrock (for example GPT-6 Sol, `us-east-1`).
2. Add GitHub as an OIDC identity provider: `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
3. Create an IAM role trusted only for `repo:OWNER/REPO:ref:refs/heads/<default branch>` (comment-triggered workflows always run from the default branch), with:

   ```json
   {
     "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"],
     "Resource": [
       "arn:aws:bedrock:us-east-1:ACCOUNT_ID:inference-profile/us.openai.gpt-6-sol",
       "arn:aws:bedrock:*::foundation-model/openai.gpt-6-sol*"
     ]
   }
   ```

   plus `bedrock:ListInferenceProfiles` on `*`.

**GitHub**: add the role ARN as the Actions secret `AWS_ROLE_TO_ASSUME`, copy [`examples/codex-review.yml`](examples/codex-review.yml) to `.github/workflows/`, set `BASE_BRANCH`, and merge it to the default branch.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `aws-role-to-assume` | required | Role ARN |
| `aws-region` | `us-east-1` | |
| `model` | `us.openai.gpt-6-sol` | Bedrock model or inference profile ID |
| `model-provider` | `amazon-bedrock-runtime` | `amazon-bedrock` for Mantle (in-region IDs such as `openai.gpt-6-sol`; needs `bedrock-mantle:CreateInference`) |
| `reasoning-effort` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `base-ref` | required | e.g. `origin/release`; the checkout needs full history (`fetch-depth: 0`) |
| `pr-number`, `head-sha` | required | From the gate job |
| `working-directory` | `.` | Where the PR is checked out |
| `sandbox` | `read-only` | Codex sandbox for commands it runs while reviewing |
| `github-token` | `github.token` | Needs `pull-requests: write` |
| `title` | `Codex review` | Review heading |
| `codex-version` | `0.159.1` | Pinned CLI version |

## Models on Bedrock

| Model ID (`amazon-bedrock-runtime`) | Per 1M tokens (input / cached / output) | Use |
|---|---|---|
| `us.openai.gpt-6-sol` | $2.00 / $0.20 / $10.00 | Default: strong reviews at moderate cost |
| `us.openai.gpt-6-astra` | $10.00 / $1.00 / $50.00 | Hardest PRs |
| `us.openai.gpt-6-luna` | $0.10 / $0.01 / $0.50 | High volume, lighter reviews |

Rates are OpenAI's standard API prices, which Bedrock matches; input above 272K tokens is billed at the long-context rate.

## Trust boundary

Use this for trusted contributors only. The review job holds model-invocation credentials and checks out PR code (it never builds or runs it). Fork PRs are skipped. Do not switch to `pull_request_target`.

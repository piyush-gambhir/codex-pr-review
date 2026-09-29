#!/usr/bin/env bash
# Run the action's steps locally against a real pull request, the same order
# action.yml runs them: progress note, Codex config, `codex exec review`,
# publish, then clear the note (or post a failure note).
#
# Uses your local Codex ChatGPT login (~/.codex/auth.json), so no API key or
# CI minutes are needed. Posts to the PR with GH_TOKEN.
#
#   GH_TOKEN=$(gh auth token --user <you>) scripts/dev/e2e_local.sh <owner/repo> <pr-number>
#
# Any environment variable the scripts read (MAX_PRIORITY, POST_MODE,
# REVIEW_INSTRUCTIONS, FAIL_ON_PRIORITY, HIDE_PREVIOUS, ...) can be set to
# override the defaults below. DRY_RUN=1 skips everything that posts.
#
# CHECK_RUN=1 also runs the check-run steps; a personal token cannot create
# check runs, so that only exercises the 403 warning. SARIF is always written
# to the run directory (SARIF_FILE overrides the path), dry runs included.
set -euo pipefail

repo="${1:?usage: e2e_local.sh <owner/repo> <pr-number>}"
pr="${2:?usage: e2e_local.sh <owner/repo> <pr-number>}"
: "${GH_TOKEN:?set GH_TOKEN to a token that can comment on $repo}"

root="$(cd "$(dirname "$0")/../.." && pwd)"
dev="$root/.dev"
codex_version="${CODEX_VERSION:-$(sed -n 's/^    default: "\([0-9.]*\)"$/\1/p' "$root/action.yml" | tail -1)}"
mkdir -p "$dev"

# Codex CLI, cached per version. npm skips the platform binary when install
# scripts are off, so install it explicitly.
cli="$dev/codex-cli-$codex_version"
if [ ! -x "$cli/node_modules/.bin/codex" ]; then
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) platform=darwin-arm64 ;;
    Darwin-x86_64) platform=darwin-x64 ;;
    Linux-x86_64) platform=linux-x64 ;;
    Linux-aarch64) platform=linux-arm64 ;;
    *) echo "unsupported platform" >&2; exit 1 ;;
  esac
  npm install --silent --prefix "$cli" --no-audit --no-fund \
    "@openai/codex@$codex_version" "@openai/codex-$platform@npm:@openai/codex@$codex_version-$platform"
fi
codex="$cli/node_modules/.bin/codex"

# PR checkout with full history, like actions/checkout with fetch-depth: 0.
pr_json="$(gh api "repos/$repo/pulls/$pr")"
head_sha="$(jq -r .head.sha <<<"$pr_json")"
base_branch="$(jq -r .base.ref <<<"$pr_json")"
checkout="$dev/checkouts/${repo//\//_}"
if [ ! -d "$checkout/.git" ]; then
  git clone -q "https://x-access-token:$GH_TOKEN@github.com/$repo.git" "$checkout"
fi
git -C "$checkout" fetch -q origin "+refs/heads/*:refs/remotes/origin/*" "+refs/pull/$pr/head:refs/remotes/origin/pr-$pr"
git -C "$checkout" checkout -q --detach "$head_sha"

run="$dev/runs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$run"
export GITHUB_REPOSITORY="$repo" PR_NUMBER="$pr" HEAD_SHA="$head_sha"
export BASE_REF="${BASE_REF:-origin/$base_branch}"
export REVIEW_TITLE="${REVIEW_TITLE:-Codex review}"
export MODEL="${MODEL:-local ChatGPT login}" LABEL="${LABEL:-local e2e}" REASONING_EFFORT="${REASONING_EFFORT:-medium}"
export RUN_URL="${RUN_URL:-https://github.com/$repo/pull/$pr}"
export RERUN_HINT="${RERUN_HINT:-local e2e run}"
export RUNNER_TEMP="$run" GITHUB_OUTPUT="$run/output" GITHUB_STEP_SUMMARY="$run/summary.md"
export GITHUB_WORKSPACE="$checkout" REVIEW_WORKSPACE="$checkout"
export POST_MODE="${POST_MODE:-review}" HIDE_PREVIOUS="${HIDE_PREVIOUS:-true}"
export SUMMARY_FILE="$run/codex-review-summary.md"
export SARIF_FILE="${SARIF_FILE:-$run/codex-review.sarif}" CODEX_VERSION="$codex_version"
: > "$GITHUB_OUTPUT"
echo "run dir: $run"

post() { [ "${DRY_RUN:-0}" = "1" ] || "$@"; }
output() { sed -n "s/^$1=//p" "$GITHUB_OUTPUT" | tail -1; }

post python3 "$root/scripts/status.py" start
export STATUS_COMMENT_ID="$(output status-comment-id)"

if [ "${CHECK_RUN:-0}" = "1" ]; then
  post python3 "$root/scripts/checks.py" start
  export CHECK_RUN_ID="$(output check-run-id)"
fi

# Codex home with the local login; config comes from write_config.py. The
# ChatGPT login picks its own model, so the model line is dropped.
export CODEX_HOME="$run/codex-home"
mkdir -p "$CODEX_HOME"
cp "$HOME/.codex/auth.json" "$CODEX_HOME/"
CODEX_PROVIDER=openai MODEL=unused SANDBOX=read-only python3 "$root/scripts/write_config.py"
sed -i.bak '/^model = /d' "$CODEX_HOME/config.toml" && rm -f "$CODEX_HOME/config.toml.bak"

status=0
(cd "$checkout" && "$codex" exec review --base "$BASE_REF" --ephemeral --json \
  -o "$run/codex-review.md" < /dev/null > "$run/codex-review-events.jsonl" 2> "$run/codex.stderr") || status=$?
rm -f "$CODEX_HOME/auth.json"
echo "codex exit: $status"
export REVIEW_FILE="$run/codex-review.md" EVENTS_FILE="$run/codex-review-events.jsonl"

if [ "$status" -ne 0 ] || [ ! -s "$REVIEW_FILE" ]; then
  post python3 "$root/scripts/status.py" fail
  # No findings file, so the check run reports the failure.
  [ "${CHECK_RUN:-0}" = "1" ] && post python3 "$root/scripts/checks.py" finish
  exit 1
fi

# SARIF is a local file, so it is written in dry runs too.
extras() {
  FINDINGS_FILE="$(output findings-file)" python3 "$root/scripts/sarif.py"
  if [ "${CHECK_RUN:-0}" = "1" ]; then
    FINDINGS_FILE="$(output findings-file)" post python3 "$root/scripts/checks.py" finish
  fi
}

if [ "${DRY_RUN:-0}" = "1" ]; then
  POST_MODE=none python3 "$root/scripts/publish_review.py" || true
  extras
  echo "--- summary"; cat "$GITHUB_STEP_SUMMARY"
  exit 0
fi

publish=0
python3 "$root/scripts/publish_review.py" || publish=$?
if grep -q '^posted=true' "$GITHUB_OUTPUT"; then
  python3 "$root/scripts/status.py" done
else
  python3 "$root/scripts/status.py" fail
fi
extras
echo "--- outputs"; cat "$GITHUB_OUTPUT"
exit "$publish"

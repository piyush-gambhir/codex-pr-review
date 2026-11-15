#!/usr/bin/env bash






























set -euo pipefail

repo="${1:?usage: e2e_local.sh <owner/repo> <pr-number>}"
pr="${2:?usage: e2e_local.sh <owner/repo> <pr-number>}"
: "${GH_TOKEN:?set GH_TOKEN to a token that can comment on $repo}"

root="$(cd "$(dirname "$0")/../.." && pwd)"
dev="$root/.dev"

codex_version="${CODEX_VERSION:-$(awk '/^  codex-version:/{f=1} f && /^    default:/{gsub(/"/, "", $2); print $2; exit}' "$root/action.yml")}"
mkdir -p "$dev"



cli="$dev/codex-cli-$codex_version"
if [ ! -x "$cli/node_modules/.bin/codex" ]; then
  mkdir -p "$cli"
  pnpm --dir "$cli" add --silent --ignore-scripts "@openai/codex@$codex_version"
fi
codex="$cli/node_modules/.bin/codex"


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
export SUGGESTIONS="${SUGGESTIONS:-true}"
export INCLUDE_PATHS="${INCLUDE_PATHS:-}" EXCLUDE_PATHS="${EXCLUDE_PATHS:-}"
export MAX_CHANGED_LINES="${MAX_CHANGED_LINES:-}" LARGE_PR="${LARGE_PR:-warn}"



export LABELS="${LABELS:-true}" REVIEW_EVENT="${REVIEW_EVENT:-COMMENT}"
export FAIL_ON_VERDICT="${FAIL_ON_VERDICT:-}"
export HEALTH_FILE="$run/codex-review-health.json"
export COVERAGE_FILE="${COVERAGE_FILE:-$run/codex-review-coverage.json}"


export ICONS="${ICONS:-true}" ICON_BASE_URL="${ICON_BASE_URL:-}"


export PROVIDER="${PROVIDER:-openai}" MAX_PRIORITY="${MAX_PRIORITY:-P3}"
export REVIEW_INSTRUCTIONS="${REVIEW_INSTRUCTIONS:-}" REVIEW_INSTRUCTIONS_FILE="${REVIEW_INSTRUCTIONS_FILE:-}"
export SKIP_UNCHANGED="${SKIP_UNCHANGED:-true}"
case "${FORCE:-}" in 1|true|yes) export FORCE=true ;; *) export FORCE=false ;; esac

case "${FULL:-}" in 1|true|yes) export REVIEW_MODE=full ;; *) export REVIEW_MODE="${REVIEW_MODE:-single}" ;; esac
export SHARD_LINES="${SHARD_LINES:-2500}" MAX_SHARDS="${MAX_SHARDS:-24}"
export MAX_PARALLEL="${MAX_PARALLEL:-4}"
export PASS_TIMEOUT_MINUTES="${PASS_TIMEOUT_MINUTES:-10}" REVIEW_BUDGET_MINUTES="${REVIEW_BUDGET_MINUTES:-20}"
export COVERAGE_FILE="$run/codex-review-coverage.json"
export CODEX_HOMES_FILE="$run/codex-review-homes.txt"
: > "$GITHUB_OUTPUT"
echo "run dir: $run"

post() { [ "${DRY_RUN:-0}" = "1" ] || "$@"; }
output() { sed -n "s/^$1=//p" "$GITHUB_OUTPUT" | tail -1; }



(cd "$checkout" && python3 "$root/scripts/filters.py")
export SIZE_NOTE="$(sed -n 's/^note=//p' "$GITHUB_OUTPUT" | tail -1)"

export CHANGED_LINES="$(sed -n 's/^changed-lines=//p' "$GITHUB_OUTPUT" | tail -1)"
export REVIEW_MODE="$(sed -n 's/^review-mode=//p' "$GITHUB_OUTPUT" | tail -1)"
if [ "$(sed -n 's/^skip=//p' "$GITHUB_OUTPUT" | tail -1)" = "true" ]; then
  echo "skipping the review: over max-changed-lines"
  CHANGED_LINES="$(sed -n 's/^changed-lines=//p' "$GITHUB_OUTPUT" | tail -1)" \
    post python3 "$root/scripts/status.py" skip
  exit 0
fi



export STATE_FILE="$run/codex-review-state.json"
export INCREMENTAL="${INCREMENTAL:-false}" RESOLVE_FIXED_THREADS="${RESOLVE_FIXED_THREADS:-true}"
(cd "$checkout" && python3 "$root/scripts/history.py" plan)


review_base="$(output review-base)"
export PREVIOUS_SHA="$(output previous-sha)"
export INCREMENTAL_NOTE="$(sed -n 's/^note=//p' "$GITHUB_OUTPUT" | tail -1)"


post python3 "$root/scripts/rereview.py" check
if [ "$(output skipped)" = "true" ]; then
  echo "skipping the review: $(output skip-reason) ($(output review-url))"
  exit 0
fi

post python3 "$root/scripts/status.py" start
export STATUS_COMMENT_ID="$(output status-comment-id)"

if [ "${CHECK_RUN:-0}" = "1" ]; then
  post python3 "$root/scripts/checks.py" start
  export CHECK_RUN_ID="$(output check-run-id)"
fi



export CODEX_HOME="$run/codex-home"
mkdir -p "$CODEX_HOME"
cp "$HOME/.codex/auth.json" "$CODEX_HOME/"
CODEX_PROVIDER=openai MODEL=unused SANDBOX=read-only python3 "$root/scripts/write_config.py"
sed -i.bak '/^model = /d' "$CODEX_HOME/config.toml" && rm -f "$CODEX_HOME/config.toml.bak"

status=0
export REVIEW_FILE="$run/codex-review.md" EVENTS_FILE="$run/codex-review-events.jsonl"
if [ "$REVIEW_MODE" = "full" ]; then


  (cd "$checkout" && BASE_REF="$review_base" CODEX_BIN="$codex" \
    python3 "$root/scripts/full_review.py" 2> "$run/codex.stderr") || status=$?
else

  (cd "$checkout" && "$codex" exec review --base "$review_base" --json \
    -o "$REVIEW_FILE" < /dev/null > "$EVENTS_FILE" 2> "$run/codex.stderr") || status=$?
fi

find "$run" -name auth.json -delete 2> /dev/null || true
echo "codex exit: $status"

if [ "$status" -ne 0 ] || [ ! -s "$REVIEW_FILE" ]; then
  post python3 "$root/scripts/status.py" fail

  [ "${CHECK_RUN:-0}" = "1" ] && post python3 "$root/scripts/checks.py" finish
  exit 1
fi




PRICING="${PRICING:-{\"local ChatGPT login\": [2, 0.2, 10]}}" python3 "$root/scripts/usage.py"
export USAGE_TEXT="$(output usage-text)"



(cd "$checkout" && BASE_REF="$review_base" python3 "$root/scripts/coverage.py")


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

#!/usr/bin/env python3
"""Real token usage and a cost estimate for one review run.

Codex's `turn.completed` event reports all-zero usage in review mode, but the
session rollout it writes under `$CODEX_HOME/sessions/**/rollout-*.jsonl` (when
the run is not `--ephemeral`) records the real counts twice:

    {"type": "token_usage_record", "payload": {"usage": {...},
     "thread_token_usage": {"input_tokens": ..., "cached_input_tokens": ...,
     "output_tokens": ..., "reasoning_output_tokens": ..., ...}}}
    {"type": "event_msg", "payload": {"type": "token_count",
     "info": {"total_token_usage": {...}, "last_token_usage": {...}}}}

`token_usage_record.payload.usage` is one model request, the `thread_`/`total_`
variants are running totals, and `output_tokens` already includes
`reasoning_output_tokens` (total_tokens == input_tokens + output_tokens).

Totals and a price-table estimate are written to GITHUB_OUTPUT; anything
missing or unpriced is simply left out, so a review never fails over usage.
Standard library only.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

# Per 1M tokens: (input, cached input, output). Reasoning bills as output.
# Standard OpenAI API rates, which Bedrock matches.
PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
}
# Requests with more input than this bill at the long-context rate.
LONG_CONTEXT_TOKENS = 272_000
LONG_INPUT_RATE = 2.0
LONG_OUTPUT_RATE = 1.5
# Bedrock dresses the same models up as `us.openai.gpt-6-sol` or `openai.gpt-6-sol`.
REGION_PREFIXES = ("us.", "eu.", "apac.", "global.")


# Reading the rollout ---------------------------------------------------------


def rollouts(sessions) -> list[pathlib.Path]:
    """Every rollout under one `sessions` directory or a list of them.

    A full review runs each pass in its own `CODEX_HOME`, so the cost of the run
    is the cost of all of them together (see full_review.py).
    """
    if isinstance(sessions, (str, pathlib.Path)):
        sessions = [sessions]
    found: list[pathlib.Path] = []
    for directory in sessions:
        directory = pathlib.Path(directory)
        if directory.is_dir():
            found.extend(sorted(directory.rglob("rollout-*.jsonl")))
    return found


def usage_events(sessions) -> list[tuple[pathlib.Path, dict, dict | None]]:
    """(rollout file, running total, single request) per usage event, in file order."""
    events = []
    for path in rollouts(sessions):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") or {}
            if event.get("type") == "token_usage_record":
                events.append((path, payload.get("thread_token_usage") or {}, payload.get("usage")))
            elif payload.get("type") == "token_count":
                info = payload.get("info") or {}
                events.append((path, info.get("total_token_usage") or {}, info.get("last_token_usage")))
    return events


def collect(sessions) -> tuple[dict[str, int], list[dict]]:
    """Totals across every rollout, plus each model request seen.

    Running totals are per rollout file (one thread), so the last one in each
    file is taken and those are summed. Requests are de-duplicated within a file,
    because `token_usage_record` and `token_count` report the same request twice;
    two passes that happen to use the same tokens are still two requests.
    """
    per_file: dict[pathlib.Path, dict] = {}
    last: dict[pathlib.Path, dict] = {}
    requests: list[dict] = []
    for path, total, request in usage_events(sessions):
        if total:
            per_file[path] = total
        if request and request != last.get(path):
            requests.append(request)
            last[path] = request
    totals = {key: 0 for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}
    for total in per_file.values():
        for key in totals:
            totals[key] += int(total.get(key) or 0)
    return totals, requests


# Pricing ---------------------------------------------------------------------


def normalise_model(model: str) -> str:
    """`us.openai.gpt-6-sol` and `openai.gpt-6-sol` are both `gpt-6-sol`."""
    name = (model or "").strip().lower()
    for prefix in REGION_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.startswith("openai."):
        name = name[len("openai."):]
    return name


def load_prices(override: str) -> dict[str, tuple[float, float, float]]:
    """The built-in table, with any `pricing` input merged over it."""
    prices = dict(PRICES)
    override = (override or "").strip()
    if not override:
        return prices
    try:
        extra = json.loads(override)
        for model, rates in extra.items():
            prices[normalise_model(model)] = tuple(float(rate) for rate in rates)[:3]
    except (ValueError, TypeError, AttributeError) as error:
        print(f"::warning::Ignoring the pricing input: {error}")
    return prices


def request_cost(usage: dict, rates: tuple[float, float, float]) -> float:
    cached = int(usage.get("cached_input_tokens") or 0)
    fresh = max(int(usage.get("input_tokens") or 0) - cached, 0)
    output = int(usage.get("output_tokens") or 0)
    input_rate, cached_rate, output_rate = rates
    if cached + fresh > LONG_CONTEXT_TOKENS:
        input_rate *= LONG_INPUT_RATE
        cached_rate *= LONG_INPUT_RATE
        output_rate *= LONG_OUTPUT_RATE
    return (fresh * input_rate + cached * cached_rate + output * output_rate) / 1_000_000


def estimate_cost(totals: dict[str, int], requests: list[dict], rates: tuple[float, float, float]) -> float:
    """Sum the per-request cost, so long-context requests are priced correctly."""
    if requests:
        return sum(request_cost(usage, rates) for usage in requests)
    return request_cost(totals, rates)


# Rendering -------------------------------------------------------------------


def format_cost(cost: float) -> str:
    return f"~${cost:.2f}" if cost >= 0.005 else f"~${cost:.4f}"


def usage_text(totals: dict[str, int], cost: float | None) -> str:
    """The meta-line fragment, e.g. `36,615 input (29,312 cached) + 926 output tokens \u00b7 ~$0.03`."""
    text = (
        f"{totals['input_tokens']:,} input ({totals['cached_input_tokens']:,} cached)"
        f" + {totals['output_tokens']:,} output tokens"
    )
    return f"{text} \u00b7 {format_cost(cost)}" if cost is not None else text


def set_output(key: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"{key}={value}\n")


# Main ------------------------------------------------------------------------


def session_dirs(env: dict[str, str]) -> list[pathlib.Path]:
    """The `sessions` directory of every Codex home this run wrote rollouts to.

    A single review has one; a full review lists its per-pass homes in the file
    `CODEX_HOMES_FILE` names, so the cost covers every pass.
    """
    listed = (env.get("CODEX_HOMES_FILE") or "").strip()
    homes: list[str] = []
    if listed and pathlib.Path(listed).is_file():
        homes = [line.strip() for line in
                 pathlib.Path(listed).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [pathlib.Path(home) / "sessions" for home in homes or [env.get("CODEX_HOME", ".")]]


def main() -> int:
    env = os.environ
    sessions = session_dirs(dict(env))
    totals, requests = collect(sessions)
    if not totals or not totals["input_tokens"] + totals["output_tokens"]:
        print("Usage: unknown (no token counts in the Codex session rollout).")
        return 0

    model = normalise_model(env.get("MODEL", ""))
    rates = load_prices(env.get("PRICING", "")).get(model)
    cost = estimate_cost(totals, requests, rates) if rates else None
    if rates is None and model:
        print(f"::notice::No price for model '{model}'; showing tokens without a cost estimate.")

    print(
        f"Usage over {len(requests)} request(s): {totals['input_tokens']} input"
        f" ({totals['cached_input_tokens']} cached), {totals['output_tokens']} output"
        f" ({totals['reasoning_output_tokens']} reasoning)"
        + (f", estimated {format_cost(cost)}" if cost is not None else "")
    )
    set_output("input-tokens", str(totals["input_tokens"]))
    set_output("cached-input-tokens", str(totals["cached_input_tokens"]))
    set_output("output-tokens", str(totals["output_tokens"]))
    if cost is not None:
        set_output("estimated-cost-usd", f"{cost:.4f}")
    set_output("usage-text", usage_text(totals, cost))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Usage is a nice-to-have; it must never fail a review that went fine.
        print(f"::warning::Could not read Codex usage: {error}")
        sys.exit(0)

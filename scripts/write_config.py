#!/usr/bin/env python3
"""Write the Codex config.toml for one review run.

Reads settings from environment variables set by action.yml, so that
multi-line review instructions and raw config never pass through shell
quoting. Standard library only.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import filters  # noqa: E402

# Kept in the instructions so custom guidelines don't change the layout that
# publish_review.py parses. \u2014 is the em dash Codex uses as a separator.
FORMAT_GUARD = (
    "Keep Codex's standard review output format: a short summary, then a line "
    "'Full review comments:' followed by one entry per finding written as "
    "'- [P<priority>] <title> \u2014 <path>:<start>-<end>' with the explanation "
    "on the following lines. Do not add prefixes before the priority tag."
)

# Asked for when the `suggestions` input is on. publish_review.py lifts the block
# out of the finding and posts it as a GitHub suggestion when the inline comment
# covers exactly the flagged lines, so the block must replace those lines and
# nothing else.
SUGGESTION_GUIDANCE = (
    "When a finding's fix is a small, self-contained replacement of the exact "
    "lines you flagged, end that finding's explanation with a fenced code block "
    "tagged 'suggestion' holding the complete replacement for those lines: the "
    "full new text of lines <start> to <end> with their original indentation, "
    "and nothing else (no diff markers, no surrounding lines, no commentary). "
    "Include it only when applying that block on its own fully fixes the issue "
    "and the flagged range covers every line that has to change. Otherwise "
    "describe the fix in prose and use no 'suggestion' block at all."
)


def toml_string(value: str) -> str:
    """Encode a TOML basic string (JSON escapes are valid TOML, except DEL)."""
    return json.dumps(value.replace("\x7f", ""), ensure_ascii=False)


def load_instructions(env: dict[str, str]) -> str:
    parts = []
    inline = env.get("REVIEW_INSTRUCTIONS", "").strip()
    if inline:
        parts.append(inline)
    file_path = env.get("REVIEW_INSTRUCTIONS_FILE", "").strip()
    if file_path:
        path = pathlib.Path(env.get("GITHUB_WORKSPACE", ".")) / file_path
        if not path.is_file():
            raise SystemExit(f"::error::review-instructions-file not found: {file_path}")
        parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(p for p in parts if p)


def build_config(env: dict[str, str]) -> str:
    lines = [
        f"model_provider = {toml_string(env['CODEX_PROVIDER'])}",
        f"model = {toml_string(env['MODEL'])}",
        f"model_reasoning_effort = {toml_string(env['REASONING_EFFORT'])}",
        f"sandbox_mode = {toml_string(env['SANDBOX'])}",
        'approval_policy = "never"',
    ]
    blocks = []
    instructions = load_instructions(env)
    if instructions:
        blocks.append(f"Repository review guidelines:\n\n{instructions}")
    excluded = filters.parse_patterns(env.get("EXCLUDE_PATHS", ""))
    if excluded:
        # Findings in these files are dropped when publishing anyway, so telling
        # Codex up front saves it the effort. The filter is still what enforces it.
        listed = ", ".join(excluded)
        blocks.append(
            "Do not review files whose path matches any of these patterns, and "
            f"report no findings in them: {listed}."
        )
    if env.get("SUGGESTIONS", "").strip().lower() == "true":
        blocks.append(SUGGESTION_GUIDANCE)
    if blocks:
        blocks.append(FORMAT_GUARD)
        guidelines = "\n\n".join(blocks)
        lines.append(f"developer_instructions = {toml_string(guidelines)}")
    extra = env.get("CODEX_CONFIG", "").strip()
    if extra:
        # Top-level keys must come before any table, so extra config goes here.
        lines += ["", "# From the codex-config input", extra]
    lines += [
        "",
        "# Commands Codex runs while reviewing get only a minimal environment,",
        "# never the API key or cloud credentials the review step holds.",
        "[shell_environment_policy]",
        'inherit = "core"',
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    home = pathlib.Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(build_config(dict(os.environ)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

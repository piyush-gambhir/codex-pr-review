#!/usr/bin/env python3
"""Suggested fixes carried inside a Codex finding.

Codex is asked (see the guidance in write_config.py) to put a small,
self-contained fix into the finding's explanation as a fenced block tagged
`suggestion`, holding the full replacement for the flagged lines. This module
pulls that block back out of the body and renders it two ways:

* as a real GitHub suggestion, which the reviewer can commit with one click,
  but which GitHub applies to the lines the comment is anchored to;
* as an ordinary labelled code block, used whenever those lines would not be
  exactly the lines the fix replaces (or when suggestions are turned off), so a
  fix is never applied to the wrong place.

Standard library only.
"""

from __future__ import annotations

import re

LABEL = "Suggested fix"
# Shown next to a finding in the issues table when it carries a suggestion.
TABLE_MARKER = "\U0001f4a1"

# The fence may be indented: finding bodies are only dedented by their common
# indent, so a block nested under a list item still starts a few spaces in.
# A longer run of the same fence character closes the block, as in CommonMark.
BLOCK_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*suggestion[ \t]*$"
    r"(?P<code>.*?)"
    r"^[ \t]*(?P=fence)(?P<extra>[`~]*)[ \t]*$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def dedent(code: str, indent: int) -> str:
    """Drop `indent` leading whitespace characters, keeping the code's own shape."""
    if not indent:
        return code
    lines = []
    for line in code.splitlines():
        head = line[:indent]
        lines.append(line[indent:] if head.strip() == "" else line.lstrip())
    return "\n".join(lines)


def extract(body: str) -> tuple[str, str]:
    """Split a finding body into (body without the block, replacement code).

    Only the first `suggestion` block is taken: a finding has one range, so a
    second block could not be anchored anywhere sensible.
    """
    match = BLOCK_RE.search(body)
    if not match:
        return body, ""
    code = dedent(match["code"].lstrip("\n").rstrip(), len(match["indent"]))
    if not code.strip():
        return body, ""
    rest = body[: match.start()].rstrip() + "\n\n" + body[match.end():].lstrip()
    return rest.strip(), code


def fence(code: str) -> str:
    """A backtick fence long enough to wrap code that itself contains fences."""
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


def github_block(code: str) -> str:
    """A real GitHub suggestion, applied to the comment's anchored lines."""
    mark = fence(code)
    return f"{mark}suggestion\n{code}\n{mark}"


def plain_block(code: str, note: str = "") -> str:
    """The same fix as an ordinary code block, which GitHub cannot apply."""
    mark = fence(code)
    heading = f"**{LABEL}**" + (f" ({note})" if note else "")
    return f"{heading}\n\n{mark}\n{code}\n{mark}"

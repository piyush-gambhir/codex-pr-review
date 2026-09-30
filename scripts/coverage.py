#!/usr/bin/env python3
"""What the reviewer actually looked at, read back out of its session rollouts.

Codex decides for itself what to read. A review that reports nothing in a file
may have studied it and found nothing, or may never have opened it, and the
review message cannot tell the two apart. The session rollout can: every command
Codex runs is recorded there, with its output.

Three shapes carry that, all seen in real `codex exec review` rollouts:

    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
      "type": "CommandExecution", "command": ["/bin/zsh", "-lc", "nl -ba a.ts"],
      "cwd": "file:///checkout", "stdout": "...",
      "parsed_cmd": [{"type": "read", "cmd": "nl -ba a.ts", "path": "a.ts"}]}}}
    {"type": "response_item", "payload": {"type": "custom_tool_call",
      "name": "exec", "call_id": "call_1",
      "input": "text(await tools.exec_command({cmd:\\"cat a.ts\\"}));"}}
    {"type": "response_item", "payload": {"type": "custom_tool_call_output",
      "call_id": "call_1", "output": [{"type": "input_text", "text": "..."}]}}

A file counts as inspected when Codex read the file itself (`cat`, `sed -n`,
`nl`, `head`, a file-reading tool call) or read a diff that contains it. Diffs
are counted from the command's captured output, by the `diff --git a/x b/x`
headers in it, so a diff the model only saw the first few thousand tokens of
counts for the files that really reached it and no further. Listings and
searches (`ls`, `rg`, `grep`, `find`, `git diff --stat`) are not inspection.

The result is written to the coverage file the action publishes:

    {"mode": "single"|"full", "complete": bool, "files_total": int,
     "files_inspected": int, "uncovered": [path], "shards": int, "passes": int}

"Inspected" is an honest floor, not a guarantee: it says the reviewer had the
file in front of it, not that every bug in it was found.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import filters  # noqa: E402
import shards  # noqa: E402

# Commands that put a file's contents in front of the model.
READ_COMMANDS = frozenset((
    "cat", "bat", "nl", "head", "tail", "sed", "awk", "less", "more", "view",
    "od", "xxd", "strings", "pr", "tac",
))
# Commands that show a diff, so every file in the output has been inspected.
DIFF_SUBCOMMANDS = frozenset(("diff", "show", "log", "diff-tree"))
# ... unless the diff was asked for as a summary, which only names the files.
SUMMARY_FLAGS = frozenset((
    "--stat", "--numstat", "--shortstat", "--dirstat", "--summary",
    "--name-only", "--name-status", "--compact-summary", "--raw", "-s",
))
# Tool calls that read a file directly rather than through a shell.
READ_TOOLS = ("read_file", "readfile", "view_file", "open_file", "read_text_file", "cat_file")
PATH_KEYS = ("path", "file_path", "filename", "file", "abs_path", "absolute_path", "target_file")
# Shell wrappers: the command itself is the last argument.
SHELL_FLAGS = ("-lc", "-c", "-lic", "-ic", "--")
# `cmd:"..."` inside the JavaScript the `exec` tool takes.
EXEC_CMD_RE = re.compile(r'"?cmd"?\s*:\s*("(?:[^"\\]|\\.)*")')
# The headers that name a file inside a unified diff.
DIFF_HEADER_RE = re.compile(
    r"^(?:diff --git a/(?P<a>[^\r\n]+?) b/(?P<b>[^\r\n]+)"
    r"|\+\+\+ b/(?P<plus>[^\t\r\n]+)"
    r"|--- a/(?P<minus>[^\t\r\n]+))$",
    re.MULTILINE,
)


class Record:
    """One command Codex ran, with whatever of its output was recorded."""

    def __init__(self, command: str = "", output: str = "", root: str = "",
                 direct: list | None = None):
        self.command = command or ""
        self.output = output or ""
        self.root = root or ""
        # Paths a structured event named outright, with no command to parse.
        self.direct = list(direct or [])


# Reading the rollouts ---------------------------------------------------------


def _text(value) -> str:
    """Flatten the shapes a tool output arrives in into plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "output", "content", "stdout"):
            if key in value:
                return _text(value[key])
    return ""


def _unwrap(text: str) -> str:
    """Unpack the JSON envelope a tool output arrives in.

    The `exec` tool reports a command as
    `{"chunk_id": "...", "exit_code": 0, "output": "...the real output..."}`,
    so the output's own newlines are escaped until this is undone.
    """
    lines = []
    for chunk in (text or "").split("\n"):
        stripped = chunk.strip()
        if stripped.startswith("{") and '"output"' in stripped:
            try:
                envelope = json.loads(stripped)
            except ValueError:
                envelope = None
            if isinstance(envelope, dict) and isinstance(envelope.get("output"), str):
                lines.append(envelope["output"])
                continue
        lines.append(chunk)
    return "\n".join(lines)


def _command_text(command) -> str:
    """The command line out of `["bash", "-lc", "..."]` or a plain string."""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command:
        parts = [str(part) for part in command]
        if len(parts) > 1 and parts[1] in SHELL_FLAGS:
            return parts[-1]
        return " ".join(parts)
    return ""


def _root_of(cwd: str) -> str:
    """The working directory of a command, from git's `file://` form or a path."""
    if not cwd:
        return ""
    if cwd.startswith("file://"):
        return urllib.parse.unquote(urllib.parse.urlparse(cwd).path)
    return cwd


def _arguments(payload: dict) -> dict:
    raw = payload.get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _exec_commands(payload: dict) -> list:
    """Command strings inside an `exec` tool call's JavaScript input."""
    found = []
    for match in EXEC_CMD_RE.finditer(payload.get("input") or ""):
        try:
            found.append(json.loads(match.group(1)))
        except ValueError:
            continue
    return found


def records(sessions: list) -> list:
    """Every command in every rollout under the given `sessions` directories."""
    found: list = []
    outputs: dict = {}
    pending: dict = {}
    for directory in sessions:
        path = pathlib.Path(directory)
        if not path.is_dir():
            continue
        for rollout in sorted(path.rglob("rollout-*.jsonl")):
            for line in rollout.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") or {}
                kind = payload.get("type")
                if kind == "item_completed":
                    item = payload.get("item") or {}
                    if item.get("type") == "CommandExecution":
                        direct = [
                            part.get("path") for part in (item.get("parsed_cmd") or [])
                            if isinstance(part, dict) and part.get("type") == "read" and part.get("path")
                        ]
                        found.append(Record(
                            command=_command_text(item.get("command")),
                            output=item.get("stdout") or _text(item.get("aggregated_output")),
                            root=_root_of(item.get("cwd") or ""),
                            direct=direct,
                        ))
                elif kind in ("custom_tool_call", "function_call", "local_shell_call"):
                    name = (payload.get("name") or "").lower()
                    call_id = payload.get("call_id") or payload.get("id") or ""
                    commands = _exec_commands(payload)
                    arguments = _arguments(payload)
                    if not commands and arguments.get("command"):
                        commands = [_command_text(arguments["command"])]
                    if not commands and isinstance(payload.get("action"), dict):
                        commands = [_command_text(payload["action"].get("command"))]
                    direct = []
                    if any(marker in name for marker in READ_TOOLS):
                        direct = [arguments[key] for key in PATH_KEYS if arguments.get(key)]
                    for command in commands or ([""] if direct else []):
                        record = Record(command=command, direct=direct)
                        pending.setdefault(call_id, []).append(record)
                        found.append(record)
                elif kind in ("custom_tool_call_output", "function_call_output"):
                    call_id = payload.get("call_id") or ""
                    outputs[call_id] = _unwrap(_text(payload.get("output")))
                elif kind == "exec_command_begin":
                    record = Record(command=_command_text(payload.get("command")),
                                    root=_root_of(payload.get("cwd") or ""))
                    pending.setdefault(payload.get("call_id") or "", []).append(record)
                    found.append(record)
                elif kind in ("exec_command_end", "exec_command_output_delta"):
                    call_id = payload.get("call_id") or ""
                    outputs[call_id] = outputs.get(call_id, "") + _text(
                        payload.get("stdout") or payload.get("aggregated_output") or payload.get("chunk"))
    for call_id, text in outputs.items():
        for record in pending.get(call_id, []):
            record.output = record.output or text
    return found


# Matching paths ---------------------------------------------------------------


def _index(candidates) -> dict:
    """{file name: [candidate paths]}, so an absolute path is matched quickly."""
    by_name: dict = {}
    for path in candidates:
        by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    return by_name


def resolve(token: str, candidates: set, by_name: dict, roots: list) -> str:
    """The changed file a command's argument names, or "" when it names none."""
    token = (token or "").strip().strip("'\"").rstrip(",;:")
    if not token:
        return ""
    token = token.replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    if token in candidates:
        return token
    for root in roots:
        if root and token.startswith(root.rstrip("/") + "/"):
            inside = token[len(root.rstrip("/")) + 1:]
            if inside in candidates:
                return inside
    for path in by_name.get(token.rsplit("/", 1)[-1], []):
        if token == path or token.endswith("/" + path):
            return path
    return ""


def split_commands(command: str) -> list:
    """Break a shell line into the simple commands it runs, honouring quotes."""
    parts, current, quote = [], [], ""
    index, size = 0, len(command or "")
    while index < size:
        char = command[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            elif char == "\\" and index + 1 < size:
                index += 1
                current.append(command[index])
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char in ";\n&|":
            parts.append("".join(current))
            current = []
            if index + 1 < size and command[index + 1] == char:
                index += 1
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def tokenise(command: str) -> list:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _git_reads(argv: list, candidates: set, by_name: dict, roots: list) -> tuple:
    """(paths the git command shows a diff of, does it show the whole diff)."""
    rest = [token for token in argv[1:]]
    subcommand = ""
    skip_next = False
    for token in rest:
        if skip_next:
            skip_next = False
            continue
        if token in ("-C", "-c", "--git-dir", "--work-tree"):
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        subcommand = token
        break
    if subcommand not in DIFF_SUBCOMMANDS:
        return set(), False
    if subcommand == "log" and not ({"-p", "--patch", "-u"} & set(rest)):
        return set(), False
    if SUMMARY_FLAGS & set(rest):
        return set(), False
    paths = set()
    for token in rest:
        if token.startswith("-"):
            continue
        hit = resolve(token, candidates, by_name, roots)
        if hit:
            paths.add(hit)
    return paths, not paths


def diff_files(output: str, candidates: set, by_name: dict, roots: list) -> set:
    """The files a captured diff actually showed, from its own headers."""
    found = set()
    for match in DIFF_HEADER_RE.finditer(output or ""):
        for token in (match.group("a"), match.group("b"), match.group("plus"), match.group("minus")):
            hit = resolve(token, candidates, by_name, roots) if token else ""
            if hit:
                found.add(hit)
    return found


def inspected(found: list, candidates, roots: list | None = None,
              scope=None) -> set:
    """The changed files Codex read, out of `candidates`.

    `scope` is what an unrestricted `git diff` covers when its output was not
    recorded (a shard pass sees only its own shard); it defaults to everything.
    """
    candidates = set(candidates)
    scope = set(scope) if scope is not None else set(candidates)
    by_name = _index(candidates)
    seen: set = set()
    for record in found:
        where = [path for path in ([record.root] + list(roots or [])) if path]
        for path in record.direct:
            hit = resolve(path, candidates, by_name, where)
            if hit:
                seen.add(hit)
        whole_diff = showed_diff = False
        for simple in split_commands(record.command):
            argv = tokenise(simple)
            if not argv:
                continue
            name = argv[0].rsplit("/", 1)[-1]
            if name in READ_COMMANDS:
                for token in argv[1:]:
                    hit = resolve(token, candidates, by_name, where)
                    if hit:
                        seen.add(hit)
            elif name == "git":
                paths, everything = _git_reads(argv, candidates, by_name, where)
                seen |= paths
                whole_diff = whole_diff or everything
                showed_diff = showed_diff or everything or bool(paths)
        if showed_diff:
            # What the diff really showed, which a truncated one stops short of.
            seen |= diff_files(record.output, candidates, by_name, where)
            if whole_diff and not record.output:
                seen |= scope  # nothing recorded, so trust the command
    return seen & candidates


# The coverage file ------------------------------------------------------------


def report(mode: str, total, inspected_paths, shard_count: int = 1, passes: int = 1) -> dict:
    """The coverage contract other steps read. Paths are sorted, so it is stable."""
    total = list(dict.fromkeys(total))
    covered = sorted(set(inspected_paths) & set(total))
    uncovered = sorted(path for path in total if path not in set(covered))
    return {
        "mode": mode,
        "complete": not uncovered and bool(total),
        "files_total": len(total),
        "files_inspected": len(covered),
        "uncovered": uncovered,
        "shards": int(shard_count),
        "passes": int(passes),
    }


def write(path: str, data: dict) -> None:
    if not path:
        return
    file = pathlib.Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(path: str) -> dict:
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def summary(data: dict) -> str:
    """The meta-line fragment, e.g. `Coverage 224/224 files (full, 12 passes)`."""
    if not data or not data.get("files_total"):
        return ""
    passes = int(data.get("passes") or 1)
    how = data.get("mode", "single")
    detail = "%s, %d pass%s" % (how, passes, "" if passes == 1 else "es")
    return "Coverage %d/%d files (%s)" % (
        int(data.get("files_inspected") or 0), int(data["files_total"]), detail)


def session_dirs(env: dict) -> list:
    """Every `sessions` directory this run's passes wrote rollouts to."""
    listed = (env.get("CODEX_HOMES_FILE") or "").strip()
    homes = []
    if listed and pathlib.Path(listed).is_file():
        homes = [line.strip() for line in pathlib.Path(listed).read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    if not homes:
        homes = [env.get("CODEX_HOME", ".")]
    return [str(pathlib.Path(home) / "sessions") for home in homes]


def set_output(key: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write("%s=%s\n" % (key, value))


def main() -> int:
    """Write the coverage file for a single-mode run, then publish the outputs.

    A full review has already written the file (full_review.py measures each
    pass as it finishes), so this only reads it back.
    """
    env = os.environ
    path = env.get("COVERAGE_FILE", "").strip()
    data = load(path) if path else {}
    if not data:
        base = shards.merge_base(env.get("BASE_REF", "").strip() or "HEAD")
        files = shards.changed_files(base, "HEAD", ".", filters.PathFilter.from_env(dict(env)))
        paths = [item.path for item in files]
        seen = inspected(records(session_dirs(dict(env))), paths,
                         [env.get("REVIEW_WORKSPACE", ""), os.getcwd()])
        data = report(env.get("REVIEW_MODE", "single").strip().lower() or "single", paths, seen)
        write(path, data)
    print("Coverage: %d/%d changed files inspected over %d pass(es)%s"
          % (data.get("files_inspected", 0), data.get("files_total", 0), data.get("passes", 1),
             "" if data.get("complete") else "; not complete"))
    for name in sorted(data.get("uncovered") or [])[:20]:
        print("  not inspected: %s" % name)
    set_output("files-total", str(data.get("files_total", 0)))
    set_output("files-inspected", str(data.get("files_inspected", 0)))
    set_output("complete", "true" if data.get("complete") else "false")
    set_output("passes", str(data.get("passes", 1)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, shards.GitError) as error:
        # Coverage is a measurement of the review, never a reason to lose one.
        print("::warning::Could not measure review coverage: %s" % error)
        sys.exit(0)

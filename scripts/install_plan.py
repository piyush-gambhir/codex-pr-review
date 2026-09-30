#!/usr/bin/env python3
"""Plan the Codex install: what to set up, and what to key its cache on.

Three questions the install steps ask before they run:

    install_plan.py probe   does this runner need `actions/setup-node` and
                            `pnpm/action-setup`, or is what it already has
                            enough? (outputs `setup-node` and `setup-pnpm`)
    install_plan.py exact   is the codex-version input a concrete version
                            (exit 0), or something that has to be resolved
                            first (exit 1), such as `latest` or a range?
    install_plan.py key     the install's cache key, from the runner, the
                            resolved Codex version and the pnpm major
                            (outputs `codex-version` and `cache-key`)

The cache key has to name the version that actually gets installed, or a run
would restore one version's install under another version's key. `latest` is
therefore resolved by pnpm first, which is also the only way to learn the answer:
pnpm applies a minimum release age as a supply-chain delay, so the newest release
it will install is usually not the registry's `latest`. The version is read back
out of the lockfile that resolution wrote.

Probing the runner only ever skips a setup step; when anything is unclear the
step runs, which is what the action did before.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

# A concrete version, so no resolution is needed: `1.2.3`, `1.2.3-rc.1`.
EXACT_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
# Specs that only ask for "something recent", which the runner's Node may be.
OPEN_SPECS = ("lts/*", "*", "latest", "node", "current")
# `22`, `22.x`, `>=22`, `>= 22.x`: a major with nothing more specific after it.
MAJOR_SPEC_RE = re.compile(r"^(?:>=\s*)?v?(\d+)(?:\.(?:x|\*)){0,2}$")
# Node the Codex CLI's launcher is run with. The package asks for >= 16; this is
# the floor below which the runner's own Node is not used even for an open spec.
MINIMUM_NODE = 20
# Bump when what is cached changes shape, to leave older entries behind.
KEY_VERSION = "1"


def major(version: str) -> int:
    """Leading number of a version string, or None when there isn't one."""
    match = re.match(r"^\s*v?(\d+)", version or "")
    return int(match.group(1)) if match else None


def is_exact(spec: str) -> bool:
    return bool(EXACT_RE.match((spec or "").strip()))


def node_satisfied(spec: str, current: str, minimum: int = MINIMUM_NODE) -> bool:
    """Is the runner's Node already what this node-version request asks for?

    Only the open-ended forms are judged: `lts/*`, `*`, `latest`, `node` and a
    bare major such as `22`, `22.x` or `>=22`. Anything more specific (`22.11.0`,
    `lts/jod`) is left to actions/setup-node, which knows how to resolve it.
    """
    spec = (spec or "").strip().lower()
    have = major(current)
    if have is None or not spec:
        return False
    if spec in OPEN_SPECS:
        return have >= minimum
    match = MAJOR_SPEC_RE.match(spec)
    if not match:
        return False
    wanted = int(match.group(1))
    if spec.startswith(">="):
        return have >= max(wanted, minimum)
    return have == wanted and have >= minimum


def pnpm_satisfied(spec: str, current: str) -> bool:
    """Is the runner's pnpm the one the install asked for?"""
    spec, current = (spec or "").strip().lower(), (current or "").strip().lstrip("v")
    if not spec or not current:
        return False
    if is_exact(spec):
        return current == spec
    return bool(MAJOR_SPEC_RE.match(spec)) and major(current) == major(spec)


def lock_version(text: str, package: str = "@openai/codex") -> str:
    """The version pnpm resolved for a package, from the lockfile it wrote.

    The importer's entry is the resolved one:

        importers:
          .:
            dependencies:
              '@openai/codex':
                specifier: ^0.158.0
                version: 0.158.0
    """
    lines = (text or "").splitlines()
    wanted = f"'{package}':"
    for index, line in enumerate(lines):
        if line.strip() not in (wanted, f'"{package}":', f"{package}:"):
            continue
        for follow in lines[index + 1:index + 6]:
            if follow.strip() and not follow.startswith((" ", "\t")):
                break
            found = re.match(r"\s+version:\s*(\S+)\s*$", follow)
            if found:
                return found.group(1).strip("'\"")
    return ""


def slug(value: str) -> str:
    """Cache-key-safe: no commas, no spaces, nothing surprising."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()) or "unknown"


def cache_key(os_name: str, arch: str, version: str, pnpm_version: str) -> str:
    """Key for one installed Codex CLI: same runner, same version, same pnpm."""
    pnpm = major(pnpm_version)
    return "-".join([
        "codex-cli", KEY_VERSION, slug(os_name), slug(arch), slug(version),
        "pnpm" + (str(pnpm) if pnpm is not None else slug(pnpm_version)),
    ])


def set_output(key: str, value: str) -> None:
    print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"{key}={value}\n")


def probe(env: dict) -> int:
    """Decide whether the two setup actions have anything left to do."""
    spec, current = env.get("NODE_VERSION", ""), env.get("NODE_CURRENT", "")
    # An empty node-version has always meant "use the runner's Node as is".
    node = bool(spec.strip()) and not node_satisfied(spec, current)
    # pnpm is only kept when Node is too: a pnpm installed against the runner's
    # own Node cannot be relied on once setup-node puts a different one in PATH.
    pnpm = node or not pnpm_satisfied(env.get("PNPM_VERSION", ""), env.get("PNPM_CURRENT", ""))
    print(f"Runner has Node {current or 'none'} and pnpm {env.get('PNPM_CURRENT') or 'none'}; "
          f"asked for {spec or 'the runner default'} and pnpm {env.get('PNPM_VERSION') or 'any'}.")
    set_output("setup-node", "true" if node else "false")
    set_output("setup-pnpm", "true" if pnpm else "false")
    return 0


def key(env: dict) -> int:
    """Name the version to install and the cache key that belongs to it."""
    spec = (env.get("CODEX_VERSION") or "").strip()
    version = spec if is_exact(spec) else ""
    lockfile = (env.get("LOCKFILE") or "").strip()
    if not version and lockfile:
        path = pathlib.Path(lockfile)
        version = lock_version(path.read_text(encoding="utf-8")) if path.is_file() else ""
        if version:
            print(f"pnpm resolves '{spec}' to {version}.")
    if not version:
        # Without an exact version a key could name the wrong install, so the
        # cache is left out of this run rather than risking that.
        print(f"::warning::Could not resolve the Codex version for '{spec}'; installing without the cache.")
        set_output("codex-version", spec)
        set_output("cache-key", "")
        return 0
    set_output("codex-version", version)
    set_output("cache-key", cache_key(env.get("RUNNER_OS", ""), env.get("RUNNER_ARCH", ""),
                                      version, env.get("PNPM_VERSION", "")))
    return 0


def main(action: str) -> int:
    env = dict(os.environ)
    if action == "probe":
        return probe(env)
    if action == "exact":
        return 0 if is_exact(env.get("CODEX_VERSION", "")) else 1
    if action == "key":
        return key(env)
    print(f"::error::Unknown install-plan action '{action}'.")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))

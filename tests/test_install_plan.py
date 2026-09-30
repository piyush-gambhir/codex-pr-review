import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import install_plan as plan  # noqa: E402

# What `pnpm add --lockfile-only @openai/codex@latest` writes, trimmed. The
# importer entry is the resolved version, which is what the cache key needs.
LOCKFILE = """lockfileVersion: '9.0'

settings:
  autoInstallPeers: true

importers:

  .:
    dependencies:
      '@openai/codex':
        specifier: ^0.158.0
        version: 0.158.0

packages:

  '@openai/codex@0.158.0':
    resolution: {integrity: sha512-deadbeef}
    engines: {node: '>=16'}
    hasBin: true

  '@openai/codex@0.158.0-linux-x64':
    resolution: {integrity: sha512-deadbeef}
    cpu: [x64]
    os: [linux]
"""


class ExactVersionTest(unittest.TestCase):
    def test_concrete_versions_need_no_resolution(self):
        for spec in ("0.159.2", "1.0.0", "0.159.2-rc.1", "12.6.0"):
            self.assertTrue(plan.is_exact(spec), spec)

    def test_everything_else_has_to_be_resolved(self):
        for spec in ("latest", "^0.159.2", ">=1.2.3", "1.2", "next", "", "1.2.3 - 1.3.0"):
            self.assertFalse(plan.is_exact(spec), spec)


class LockfileTest(unittest.TestCase):
    def test_reads_the_resolved_version(self):
        self.assertEqual(plan.lock_version(LOCKFILE), "0.158.0")

    def test_missing_package_or_lockfile(self):
        self.assertEqual(plan.lock_version(""), "")
        self.assertEqual(plan.lock_version(LOCKFILE, "@openai/other"), "")
        self.assertEqual(plan.lock_version("importers:\n  .:\n    dependencies:\n"), "")


class NodeProbeTest(unittest.TestCase):
    def test_open_requests_accept_any_recent_node(self):
        for spec in ("lts/*", "*", "latest", "node", "current"):
            self.assertTrue(plan.node_satisfied(spec, "v24.5.0"), spec)
            self.assertFalse(plan.node_satisfied(spec, "v18.20.0"), spec)

    def test_a_bare_major_has_to_match(self):
        self.assertTrue(plan.node_satisfied("22", "v22.11.0"))
        self.assertTrue(plan.node_satisfied("22.x", "v22.11.0"))
        self.assertFalse(plan.node_satisfied("22", "v24.1.0"))
        self.assertTrue(plan.node_satisfied(">=22", "v24.1.0"))
        self.assertFalse(plan.node_satisfied(">=26", "v24.1.0"))

    def test_anything_specific_is_left_to_setup_node(self):
        for spec in ("22.11.0", "lts/jod", "iron", "20.0.0-nightly"):
            self.assertFalse(plan.node_satisfied(spec, "v22.11.0"), spec)

    def test_no_node_at_all(self):
        self.assertFalse(plan.node_satisfied("lts/*", ""))
        self.assertFalse(plan.node_satisfied("lts/*", "not a version"))

    def test_too_old_even_for_an_open_request(self):
        self.assertFalse(plan.node_satisfied("lts/*", "v16.20.0"))


class PnpmProbeTest(unittest.TestCase):
    def test_the_major_is_what_matters(self):
        self.assertTrue(plan.pnpm_satisfied("12", "12.6.0"))
        self.assertTrue(plan.pnpm_satisfied("12.x", "12.0.1"))
        self.assertFalse(plan.pnpm_satisfied("12", "11.9.0"))

    def test_a_pinned_pnpm_has_to_match_exactly(self):
        self.assertTrue(plan.pnpm_satisfied("12.6.0", "12.6.0"))
        self.assertFalse(plan.pnpm_satisfied("12.6.0", "12.6.1"))

    def test_nothing_installed_or_nothing_asked_for(self):
        self.assertFalse(plan.pnpm_satisfied("12", ""))
        self.assertFalse(plan.pnpm_satisfied("", "12.6.0"))
        self.assertFalse(plan.pnpm_satisfied("latest", "12.6.0"))


class CacheKeyTest(unittest.TestCase):
    def test_names_the_runner_the_version_and_the_pnpm_major(self):
        self.assertEqual(plan.cache_key("Linux", "X64", "0.159.2", "12"),
                         "codex-cli-1-Linux-X64-0.159.2-pnpm12")
        self.assertEqual(plan.cache_key("Linux", "X64", "0.159.2", "12.6.0"),
                         "codex-cli-1-Linux-X64-0.159.2-pnpm12")

    def test_a_different_runner_or_version_is_a_different_key(self):
        base = plan.cache_key("Linux", "X64", "0.159.2", "12")
        self.assertNotEqual(base, plan.cache_key("macOS", "X64", "0.159.2", "12"))
        self.assertNotEqual(base, plan.cache_key("Linux", "ARM64", "0.159.2", "12"))
        self.assertNotEqual(base, plan.cache_key("Linux", "X64", "0.159.1", "12"))
        self.assertNotEqual(base, plan.cache_key("Linux", "X64", "0.159.2", "11"))

    def test_keys_stay_safe_for_the_cache_api(self):
        key = plan.cache_key("Linux", "X64", "1.0.0 || 2.0.0", "12")
        self.assertNotIn(",", key)
        self.assertNotIn(" ", key)


def run(action, **env):
    """Run main() and return (exit code, outputs)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp, "out")
        out.touch()
        with mock.patch.dict(os.environ, {**env, "GITHUB_OUTPUT": str(out)}, clear=True):
            code = plan.main(action)
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if line)
    return code, outputs


class MainTest(unittest.TestCase):
    def test_probe_keeps_a_suitable_runtime(self):
        code, outputs = run("probe", NODE_VERSION="lts/*", NODE_CURRENT="v24.5.0",
                            PNPM_VERSION="12", PNPM_CURRENT="12.6.0")
        self.assertEqual(code, 0)
        self.assertEqual(outputs, {"setup-node": "false", "setup-pnpm": "false"})

    def test_probe_sets_up_both_when_node_is_replaced(self):
        # pnpm is not kept on its own: setup-node would move the Node under it.
        _, outputs = run("probe", NODE_VERSION="lts/*", NODE_CURRENT="v18.0.0",
                         PNPM_VERSION="12", PNPM_CURRENT="12.6.0")
        self.assertEqual(outputs, {"setup-node": "true", "setup-pnpm": "true"})

    def test_probe_keeps_the_runners_node_when_none_is_asked_for(self):
        _, outputs = run("probe", NODE_VERSION="", NODE_CURRENT="v18.0.0",
                         PNPM_VERSION="12", PNPM_CURRENT="")
        self.assertEqual(outputs, {"setup-node": "false", "setup-pnpm": "true"})

    def test_probe_on_a_bare_runner(self):
        _, outputs = run("probe", NODE_VERSION="lts/*", PNPM_VERSION="12")
        self.assertEqual(outputs, {"setup-node": "true", "setup-pnpm": "true"})

    def test_exact_exit_codes(self):
        self.assertEqual(run("exact", CODEX_VERSION="0.159.2")[0], 0)
        self.assertEqual(run("exact", CODEX_VERSION="latest")[0], 1)

    def test_key_for_a_pinned_version_needs_no_lockfile(self):
        _, outputs = run("key", CODEX_VERSION="0.159.2", RUNNER_OS="Linux",
                         RUNNER_ARCH="X64", PNPM_VERSION="12")
        self.assertEqual(outputs, {"codex-version": "0.159.2",
                                   "cache-key": "codex-cli-1-Linux-X64-0.159.2-pnpm12"})

    def test_key_for_latest_comes_from_the_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = pathlib.Path(tmp, "pnpm-lock.yaml")
            lock.write_text(LOCKFILE, encoding="utf-8")
            _, outputs = run("key", CODEX_VERSION="latest", LOCKFILE=str(lock),
                             RUNNER_OS="Linux", RUNNER_ARCH="X64", PNPM_VERSION="12")
        self.assertEqual(outputs["codex-version"], "0.158.0")
        self.assertEqual(outputs["cache-key"], "codex-cli-1-Linux-X64-0.158.0-pnpm12")

    def test_an_unresolvable_version_turns_the_cache_off(self):
        # An empty cache key skips the restore and save steps, so a run can never
        # restore one version's install under another version's key.
        _, outputs = run("key", CODEX_VERSION="latest", LOCKFILE="/nonexistent/pnpm-lock.yaml",
                         RUNNER_OS="Linux", RUNNER_ARCH="X64", PNPM_VERSION="12")
        self.assertEqual(outputs, {"codex-version": "latest", "cache-key": ""})

    def test_unknown_action(self):
        self.assertEqual(run("nope")[0], 2)


if __name__ == "__main__":
    unittest.main()

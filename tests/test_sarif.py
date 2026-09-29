import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import sarif  # noqa: E402


def finding(priority=1, start=15, end=18, title="Convert the percentage", body="Because."):
    return {"priority": priority, "title": title, "path": "src/pricing.ts", "start": start, "end": end, "body": body}


class ShapeTest(unittest.TestCase):
    def test_sarif_2_1_0_essentials(self):
        log = sarif.sarif_log([finding()])
        self.assertEqual(log["version"], "2.1.0")
        self.assertTrue(log["$schema"].endswith("sarif-schema-2.1.0.json"))
        self.assertEqual(len(log["runs"]), 1)
        run = log["runs"][0]
        self.assertEqual(run["tool"]["driver"]["name"], "Codex PR Review")
        self.assertTrue(run["tool"]["driver"]["informationUri"].startswith("https://"))
        result = run["results"][0]
        self.assertTrue(result["message"]["text"])
        self.assertTrue(result["locations"])

    def test_one_rule_per_priority_indexed_by_priority(self):
        rules = sarif.sarif_log([])["runs"][0]["tool"]["driver"]["rules"]
        self.assertEqual([r["id"] for r in rules], [f"codex-review/p{p}" for p in range(4)])
        self.assertEqual([r["defaultConfiguration"]["level"] for r in rules], ["error", "error", "warning", "note"])
        for priority in range(4):
            result = sarif.result(finding(priority))
            self.assertEqual(result["ruleIndex"], priority)
            self.assertEqual(rules[result["ruleIndex"]]["id"], result["ruleId"])

    def test_levels_mapped_from_priority(self):
        self.assertEqual([sarif.result(finding(p))["level"] for p in range(4)], ["error", "error", "warning", "note"])

    def test_region_carries_the_line_range(self):
        location = sarif.result(finding(1, 15, 18))["locations"][0]["physicalLocation"]
        self.assertEqual(location["artifactLocation"]["uri"], "src/pricing.ts")
        self.assertEqual(location["region"], {"startLine": 15, "endLine": 18})
        # Codex sometimes reports a single line, or line 0 for a whole file.
        self.assertEqual(sarif.result(finding(1, 0, 0))["locations"][0]["physicalLocation"]["region"],
                         {"startLine": 1, "endLine": 1})

    def test_message_has_the_priority_title_and_body(self):
        text = sarif.result(finding(2, body="Explanation."))["message"]["text"]
        self.assertEqual(text, "P2: Convert the percentage\n\nExplanation.")
        self.assertEqual(sarif.result(finding(2, body=""))["message"]["text"], "P2: Convert the percentage")

    def test_fingerprints_ignore_line_numbers_but_not_the_finding(self):
        same = sarif.result(finding(1, 15, 18))["partialFingerprints"]
        moved = sarif.result(finding(1, 40, 43))["partialFingerprints"]
        other = sarif.result(finding(1, 15, 18, title="Something else"))["partialFingerprints"]
        self.assertEqual(same, moved)
        self.assertNotEqual(same, other)
        self.assertEqual(list(same), [sarif.FINGERPRINT_KEY])

    def test_paths_are_relative(self):
        uri = sarif.result(dict(finding(), path="/src/pricing.ts"))["locations"][0]
        self.assertEqual(uri["physicalLocation"]["artifactLocation"]["uri"], "src/pricing.ts")


class MainTest(unittest.TestCase):
    def run_main(self, findings, **env):
        with tempfile.TemporaryDirectory() as tmp:
            out, target = pathlib.Path(tmp, "out"), pathlib.Path(tmp, "nested", "codex.sarif")
            base = {"GITHUB_OUTPUT": str(out), "SARIF_FILE": str(target), "REVIEW_TITLE": "Codex review",
                    "CODEX_VERSION": "0.159.1"}
            if findings is not None:
                findings_file = pathlib.Path(tmp, "findings.json")
                findings_file.write_text(json.dumps(findings))
                base["FINDINGS_FILE"] = str(findings_file)
            with mock.patch.dict(os.environ, {**base, **env}, clear=True):
                code = sarif.main()
            written = json.loads(target.read_text()) if target.exists() else None
            return code, written, out.read_text() if out.exists() else ""

    def test_writes_the_file_and_the_output(self):
        code, log, outputs = self.run_main([finding(1), finding(3, 22, 22)])
        self.assertEqual(code, 0)
        self.assertEqual(len(log["runs"][0]["results"]), 2)
        self.assertEqual(log["runs"][0]["tool"]["driver"]["version"], "0.159.1")
        self.assertEqual(log["runs"][0]["automationDetails"]["id"], "Codex review/")
        self.assertIn("sarif-file=", outputs)

    def test_no_findings_still_writes_a_valid_run(self):
        _, log, _ = self.run_main([])
        self.assertEqual(log["runs"][0]["results"], [])

    def test_missing_findings_file_writes_an_empty_run(self):
        _, log, _ = self.run_main(None)
        self.assertEqual(log["runs"][0]["results"], [])

    def test_no_target_writes_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sarif.main(), 0)


if __name__ == "__main__":
    unittest.main()

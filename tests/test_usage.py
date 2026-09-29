import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import usage  # noqa: E402

# Captured from `codex exec review` 0.159.1: the exec thread (no usage) and the
# review subagent thread (four requests), trimmed to the usage events.
CODEX_HOME = pathlib.Path(__file__).parent / "fixtures" / "codex-home"
SESSIONS = CODEX_HOME / "sessions"
SOL = usage.PRICES["gpt-6-sol"]


class CollectTest(unittest.TestCase):
    def test_real_rollout_totals(self):
        totals, requests = usage.collect(SESSIONS)
        self.assertEqual(totals["input_tokens"], 36615)
        self.assertEqual(totals["cached_input_tokens"], 29312)
        self.assertEqual(totals["output_tokens"], 926)
        self.assertEqual(totals["reasoning_output_tokens"], 47)

    def test_requests_are_not_double_counted(self):
        # Each request appears as both a token_usage_record and a token_count.
        _, requests = usage.collect(SESSIONS)
        self.assertEqual([r["input_tokens"] for r in requests], [7881, 8502, 9705, 10527])

    def test_totals_sum_across_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp, "sessions")
            sessions.mkdir()
            for name, count in (("rollout-a.jsonl", 10), ("rollout-b.jsonl", 32)):
                event = {"type": "token_usage_record", "payload": {"thread_token_usage": {
                    "input_tokens": count, "cached_input_tokens": 1, "output_tokens": 2, "reasoning_output_tokens": 1}}}
                pathlib.Path(sessions, name).write_text(json.dumps(event) + "\n")
            totals, _ = usage.collect(sessions)
        self.assertEqual((totals["input_tokens"], totals["output_tokens"]), (42, 4))

    def test_no_sessions_and_unparsable_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp, "sessions")
            sessions.mkdir()
            self.assertEqual(usage.collect(sessions), ({k: 0 for k in (
                "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}, []))
            pathlib.Path(sessions, "rollout-x.jsonl").write_text("not json\n{}\n")
            self.assertEqual(usage.collect(sessions)[1], [])


class PricingTest(unittest.TestCase):
    def test_bedrock_ids_map_to_the_same_model(self):
        for model in ("gpt-6-sol", "openai.gpt-6-sol", "us.openai.gpt-6-sol", "EU.OpenAI.GPT-6-Sol"):
            self.assertEqual(usage.normalise_model(model), "gpt-6-sol")

    def test_pricing_override_is_merged_and_normalised(self):
        prices = usage.load_prices('{"My-Model": [2, 0.2, 10], "us.openai.gpt-6-luna": [1, 1, 1]}')
        self.assertEqual(prices["my-model"], (2.0, 0.2, 10.0))
        self.assertEqual(prices["gpt-6-luna"], (1.0, 1.0, 1.0))
        self.assertEqual(prices["gpt-6-sol"], usage.PRICES["gpt-6-sol"])

    def test_bad_pricing_override_is_ignored(self):
        self.assertEqual(usage.load_prices("not json"), usage.PRICES)
        self.assertEqual(usage.load_prices("[1, 2]"), usage.PRICES)

    def test_cost_bills_fresh_cached_and_output_separately(self):
        # 100k fresh at $2, 100k cached at $0.20, 10k output at $10 per 1M.
        cost = usage.request_cost(
            {"input_tokens": 200_000, "cached_input_tokens": 100_000, "output_tokens": 10_000}, SOL)
        self.assertAlmostEqual(cost, 0.2 + 0.02 + 0.1)

    def test_long_context_requests_bill_at_the_higher_rate(self):
        below = usage.request_cost({"input_tokens": 272_000, "cached_input_tokens": 0, "output_tokens": 1_000}, SOL)
        above = usage.request_cost({"input_tokens": 272_001, "cached_input_tokens": 0, "output_tokens": 1_000}, SOL)
        self.assertAlmostEqual(below, 272_000 * 2.0 / 1e6 + 1_000 * 10.0 / 1e6)
        self.assertAlmostEqual(above, 272_001 * 4.0 / 1e6 + 1_000 * 15.0 / 1e6)

    def test_per_request_pricing_beats_pricing_the_totals(self):
        # Four requests well under the threshold whose totals exceed it.
        requests = [{"input_tokens": 200_000, "cached_input_tokens": 0, "output_tokens": 0}] * 4
        totals = {"input_tokens": 800_000, "cached_input_tokens": 0, "output_tokens": 0}
        self.assertAlmostEqual(usage.estimate_cost(totals, requests, SOL), 1.6)
        self.assertAlmostEqual(usage.estimate_cost(totals, [], SOL), 3.2)

    def test_real_rollout_cost(self):
        totals, requests = usage.collect(SESSIONS)
        cost = usage.estimate_cost(totals, requests, SOL)
        self.assertAlmostEqual(cost, 0.029728, places=6)


class RenderTest(unittest.TestCase):
    def test_usage_text(self):
        totals = {"input_tokens": 36615, "cached_input_tokens": 29312, "output_tokens": 926}
        self.assertEqual(
            usage.usage_text(totals, 0.029728),
            "36,615 input (29,312 cached) + 926 output tokens \u00b7 ~$0.03",
        )
        self.assertEqual(usage.usage_text(totals, None), "36,615 input (29,312 cached) + 926 output tokens")

    def test_tiny_costs_keep_a_digit(self):
        self.assertEqual(usage.format_cost(0.0), "~$0.0000")
        self.assertEqual(usage.format_cost(0.0012), "~$0.0012")
        self.assertEqual(usage.format_cost(1.5), "~$1.50")


class MainTest(unittest.TestCase):
    def run_main(self, **env):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp, "out")
            base = {"CODEX_HOME": str(CODEX_HOME), "GITHUB_OUTPUT": str(out)}
            with mock.patch.dict(os.environ, {**base, **env}, clear=True):
                code = usage.main()
            text = out.read_text() if out.exists() else ""
        return code, dict(line.split("=", 1) for line in text.splitlines())

    def test_outputs_for_a_known_model(self):
        code, outputs = self.run_main(MODEL="us.openai.gpt-6-sol")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["input-tokens"], "36615")
        self.assertEqual(outputs["cached-input-tokens"], "29312")
        self.assertEqual(outputs["output-tokens"], "926")
        self.assertEqual(outputs["estimated-cost-usd"], "0.0297")
        self.assertIn("~$0.03", outputs["usage-text"])

    def test_unknown_model_reports_tokens_without_a_cost(self):
        _, outputs = self.run_main(MODEL="gpt-9-unreleased")
        self.assertNotIn("estimated-cost-usd", outputs)
        self.assertEqual(outputs["usage-text"], "36,615 input (29,312 cached) + 926 output tokens")

    def test_pricing_input_prices_an_unknown_model(self):
        _, outputs = self.run_main(MODEL="gpt-9-unreleased", PRICING='{"gpt-9-unreleased": [2, 0.2, 10]}')
        self.assertEqual(outputs["estimated-cost-usd"], "0.0297")

    def test_missing_sessions_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, outputs = self.run_main(CODEX_HOME=tmp, MODEL="gpt-6-sol")
        self.assertEqual((code, outputs), (0, {}))


if __name__ == "__main__":
    unittest.main()

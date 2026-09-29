import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import suggestions  # noqa: E402


class ExtractTest(unittest.TestCase):
    def test_block_is_lifted_out_of_the_body(self):
        body = "Divide by 100.\n\n```suggestion\n  return Math.round(subtotal * (percent / 100));\n```\n\nSee above."
        rest, code = suggestions.extract(body)
        self.assertEqual(rest, "Divide by 100.\n\nSee above.")
        self.assertEqual(code, "  return Math.round(subtotal * (percent / 100));")

    def test_indented_fence_keeps_the_code_s_own_indentation(self):
        body = "Fix it.\n\n  ```suggestion\n  function f() {\n    return 1;\n  }\n  ```"
        rest, code = suggestions.extract(body)
        self.assertEqual(rest, "Fix it.")
        self.assertEqual(code, "function f() {\n  return 1;\n}")

    def test_tilde_fences_and_a_mixed_case_tag(self):
        rest, code = suggestions.extract("Fix.\n\n~~~Suggestion\nconst a = 1;\n~~~")
        self.assertEqual((rest, code), ("Fix.", "const a = 1;"))

    def test_longer_closing_fence(self):
        rest, code = suggestions.extract("Fix.\n\n```suggestion\nconst a = 1;\n````")
        self.assertEqual((rest, code), ("Fix.", "const a = 1;"))

    def test_other_code_blocks_are_left_alone(self):
        body = "Currently:\n\n```ts\nconst a = 1;\n```\n\nno fix available."
        self.assertEqual(suggestions.extract(body), (body, ""))

    def test_only_the_first_block_is_taken(self):
        body = "A.\n\n```suggestion\none\n```\n\nB.\n\n```suggestion\ntwo\n```"
        rest, code = suggestions.extract(body)
        self.assertEqual(code, "one")
        self.assertIn("```suggestion\ntwo\n```", rest)

    def test_empty_block_is_ignored(self):
        body = "Fix.\n\n```suggestion\n\n```"
        self.assertEqual(suggestions.extract(body), (body, ""))

    def test_no_block(self):
        self.assertEqual(suggestions.extract("Just prose."), ("Just prose.", ""))


class RenderTest(unittest.TestCase):
    def test_github_block(self):
        self.assertEqual(suggestions.github_block("const a = 1;"), "```suggestion\nconst a = 1;\n```")

    def test_plain_block_cannot_be_applied(self):
        text = suggestions.plain_block("const a = 1;", "replaces `a.ts:3`")
        self.assertNotIn("```suggestion", text)
        self.assertIn("**Suggested fix** (replaces `a.ts:3`)", text)
        self.assertIn("```\nconst a = 1;\n```", text)

    def test_fence_grows_past_backticks_in_the_code(self):
        code = "a = `````x`````"
        self.assertEqual(suggestions.fence(code), "``````")
        self.assertTrue(suggestions.github_block(code).startswith("``````suggestion\n"))

    def test_fence_default(self):
        self.assertEqual(suggestions.fence("plain"), "```")


if __name__ == "__main__":
    unittest.main()

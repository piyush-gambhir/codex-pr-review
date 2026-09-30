import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import checks  # noqa: E402
import history  # noqa: E402
import icons  # noqa: E402
import publish_review as pr  # noqa: E402
import verdict as verdicts  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "icons"
SHA = "0123456789abcdef0123456789abcdef01234567"
OLD_SHA = "89abcdef0123456789abcdef0123456789abcdef"
CDN = "https://cdn.jsdelivr.net/gh/piyush-gambhir/codex-pr-review"


class BaseUrlTest(unittest.TestCase):
    """The default URL may only ever name this public repository at a fixed ref."""

    def test_the_actions_own_tag_is_used(self):
        env = {"GITHUB_ACTION_REPOSITORY": "piyush-gambhir/codex-pr-review", "GITHUB_ACTION_REF": "v1.2.1"}
        self.assertEqual(icons.default_base_url(env), f"{CDN}@v1.2.1/icons")
        for ref in ("v1", "v1.3", "v2.0.0", "f" * 40):
            self.assertEqual(icons.resolve_ref(dict(env, GITHUB_ACTION_REF=ref)), ref)

    def test_a_branch_or_short_sha_falls_back_to_the_pinned_ref(self):
        env = {"GITHUB_ACTION_REPOSITORY": "piyush-gambhir/codex-pr-review"}
        for ref in ("main", "ui/icons-preview", "", "abc1234", "release-1", "V1", "F" * 40):
            self.assertEqual(icons.resolve_ref(dict(env, GITHUB_ACTION_REF=ref)), icons.PINNED_REF)

    def test_another_repository_or_a_local_checkout_never_leaks(self):
        # A local `./` use sets neither variable; a mirror sets a different repo.
        for env in ({}, {"GITHUB_ACTION_REF": "v1.2.1"},
                    {"GITHUB_ACTION_REPOSITORY": "acme/private-mirror", "GITHUB_ACTION_REF": "v9.9.9"}):
            url = icons.default_base_url(env)
            self.assertEqual(url, f"{CDN}@{icons.PINNED_REF}/icons")
            self.assertNotIn("private-mirror", url)

    def test_the_pinned_ref_is_an_immutable_ref(self):
        self.assertTrue(icons.TAG_RE.match(icons.PINNED_REF) or icons.SHA_RE.match(icons.PINNED_REF))

    def test_the_input_wins_and_a_trailing_slash_is_dropped(self):
        got = icons.Icons.from_env({"ICON_BASE_URL": "https://example.test/assets/icons/"})
        self.assertEqual(got.base_url, "https://example.test/assets/icons")
        self.assertIn('src="https://example.test/assets/icons/priority-p0.svg"', got.img("p0"))

    def test_icons_false_disables_every_image(self):
        off = icons.Icons.from_env({"ICONS": "false", "ICON_BASE_URL": "https://example.test/i"})
        self.assertFalse(off.enabled)
        self.assertEqual([off.img(name) for name in icons.CATALOGUE], [""] * len(icons.CATALOGUE))
        self.assertEqual(off.tagged("inline", "Inline"), "Inline")
        self.assertEqual(off.marker("suggestion", "suggested fix"), "<sub>(suggested fix)</sub>")
        self.assertFalse(icons.Icons("", True).enabled)  # nowhere to load from


class CatalogueTest(unittest.TestCase):
    def test_every_icon_exists_and_sets_one_explicit_fill(self):
        for name, (file, _) in sorted(icons.CATALOGUE.items()):
            path = ICON_DIR / file
            self.assertTrue(path.is_file(), f"{name}: icons/{file} is missing")
            svg = path.read_text(encoding="utf-8")
            self.assertRegex(svg, r'\A<svg fill="#[0-9a-f]{6}" ', f"{name} has no explicit fill")
            self.assertIn('viewBox="0 0 16 16"', svg)
            self.assertLess(len(svg), 2048, f"{name} is not tiny")

    def test_every_icon_has_meaningful_alt_text(self):
        for name, (_, alt) in icons.CATALOGUE.items():
            self.assertTrue(alt.strip(), name)
            self.assertGreater(len(alt), 2, name)
            self.assertNotIn("icon", alt.lower(), f"{name} alt describes the picture, not the meaning")

    def test_every_image_declares_the_text_size(self):
        icon = icons.Icons("https://example.test/i")
        for name in icons.CATALOGUE:
            tag = icon.img(name)
            self.assertRegex(tag, r'\A<img src="[^"]+\.svg" width="16" height="16" alt="[^"]+">\Z', name)

    def test_priority_names_cover_p0_to_p3(self):
        self.assertEqual([icons.priority(p) for p in range(4)], ["p0", "p1", "p2", "p3"])
        self.assertEqual(icons.priority(9), "p3")

    def test_the_build_recipe_matches_the_catalogue(self):
        sys.path.insert(0, str(ROOT / "scripts" / "dev"))
        import build_icons  # noqa: E402

        self.assertEqual(sorted(build_icons.RECIPE), sorted(icons.CATALOGUE))
        for name, (_, fill) in build_icons.RECIPE.items():
            light = build_icons.contrast(fill, build_icons.LIGHT_BG)
            dark = build_icons.contrast(fill, build_icons.DARK_BG)
            self.assertGreaterEqual(min(light, dark), 3.0, f"{name} ({fill}) is illegible on one theme")
            self.assertIn(fill, (ICON_DIR / icons.CATALOGUE[name][0]).read_text(encoding="utf-8"))


def finding(priority=1, title="Convert the percentage", path="src/pricing.ts", start=15, end=None,
            suggestion=""):
    item = {"priority": priority, "title": title, "path": path, "start": start, "end": end or start,
            "body": "Because the percentage is applied whole.", "suggestion": suggestion}
    item["fingerprint"] = history.fingerprint(path, title)
    return item


def everything_posted(**env):
    """Every piece of Markdown a realistic run writes, as one string."""
    base = {"GITHUB_REPOSITORY": "o/r", "HEAD_SHA": SHA, "PREVIOUS_SHA": OLD_SHA, "BASE_REF": "origin/main",
            "REVIEW_TITLE": "Codex review", "MODEL": "gpt-6.1-sol", "LABEL": "OpenAI API",
            "REASONING_EFFORT": "medium", "RUN_URL": "https://github.com/o/r/actions/runs/9",
            "USAGE_TEXT": "36,615 input (29,312 cached) + 926 output tokens · ~$0.03",
            "RERUN_HINT": "Comment `@gpt review` to re-run."}
    base.update(env)
    ctx = pr.Context(base)
    findings = [
        finding(0, "Reject a negative discount", start=8),
        finding(1, "Convert the percentage", suggestion="  const rate = percent / 100;"),
        finding(2, "Handle zero units", start=22, end=24),
        finding(3, "Name the magic number", start=31),
    ]
    ctx.resolved = [history.entry(finding(1, "Round to integer cents", start=40))]
    ctx.carried = [history.entry(finding(2, "Validate the coupon code", path="src/coupons.ts", start=8))]
    ctx.still_open = {findings[0]["fingerprint"]}
    ctx.health = verdicts.assess(findings, ctx.carried,
                                 {"draft": True, "mergeable": "CONFLICTING", "checks": "FAILURE"},
                                 previous={"hs": 30, "vd": "blocked"})
    ctx.state = history.state_marker(SHA, findings, ctx.carried, "digest", ctx.health)
    ctx.note = "1 finding outside the reviewed paths not shown."
    inline = {id(findings[0]), id(findings[1])}
    thin = verdicts.assess([], coverage={"complete": False, "files_total": 224, "files_inspected": 38})
    parts = [
        pr.body_markdown("The patch introduces four correctness issues.", findings, ctx, inline),
        pr.body_markdown("The patch introduces four correctness issues.", findings, ctx),
        pr.body_markdown("No issues found in the changes.", [], ctx),
        pr.inline_comment(findings[1], ctx, exact_range=True),
        pr.inline_comment(findings[2], ctx, exact_range=False),
        pr.details(findings[3], ctx),
        pr.verdict(findings, 1),
        pr.verdict([], 2),
        pr.verdict([], 0, thin),
        verdicts.breakdown(thin),
        checks.summary_markdown("The patch introduces four correctness issues.", findings, ctx, ctx.health),
        checks.headline(findings),
        checks.headline(findings, ctx.health),
    ]
    return "\n\n".join(parts)


def status_notes(**env):
    """The bodies status.py posts for a start, a skip and a failure."""
    import status

    base = {"GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1", "GH_TOKEN": "t", "HEAD_SHA": SHA,
            "BASE_REF": "origin/main", "REVIEW_TITLE": "Codex review", "MODEL": "gpt-6.1-sol",
            "LABEL": "OpenAI API", "REASONING_EFFORT": "medium", "RUN_URL": "https://example/run",
            "CHANGED_LINES": "1200", "MAX_CHANGED_LINES": "500", "STATUS_COMMENT_ID": "42",
            "RERUN_HINT": "Comment `@gpt review` to re-run."}
    base.update(env)
    bodies = []
    with tempfile.TemporaryDirectory() as tmp:
        events = pathlib.Path(tmp, "events.jsonl")
        events.write_text(json.dumps({"type": "error", "message": "unexpected status 401: bad key"}))
        full = dict(base, EVENTS_FILE=str(events), GITHUB_OUTPUT=str(pathlib.Path(tmp, "out")))
        for action in ("start", "skip", "fail"):
            with mock.patch.dict(os.environ, full, clear=True), \
                    mock.patch.object(status, "github",
                                      side_effect=lambda *a, **k: bodies.append((a[3] or {}).get("body", ""))
                                      or {"id": 42}):
                status.main(action)
    return [body for body in bodies if body]


# Anything a reader would call an emoji: pictographs, dingbats, symbol blocks
# and the variation selectors that turn a glyph into one. The typographic
# characters the layouts really do use are the only exceptions.
KEEP = set("\u2014\u2013\u00b7\u00a0\u2026\u2018\u2019\u201c\u201d")
EMOJI_RANGES = (
    (0x2190, 0x21FF), (0x2300, 0x23FF), (0x2460, 0x24FF), (0x25A0, 0x27BF),
    (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x3030, 0x303D), (0xFE00, 0xFE0F),
    (0x1F000, 0x1FAFF),
)


def emoji_in(text):
    found = []
    for char in text:
        if char in KEEP:
            continue
        point = ord(char)
        if any(low <= point <= high for low, high in EMOJI_RANGES):
            found.append("U+%04X" % point)
    return sorted(set(found))


class NoEmojiTest(unittest.TestCase):
    def test_a_realistic_review_has_no_emoji(self):
        self.assertEqual(emoji_in(everything_posted()), [])

    def test_text_only_mode_has_no_emoji_either(self):
        rendered = everything_posted(ICONS="false")
        self.assertEqual(emoji_in(rendered), [])
        self.assertNotIn("<img", rendered)

    def test_every_status_note_has_no_emoji(self):
        self.assertEqual(emoji_in("\n".join(status_notes())), [])
        self.assertEqual(emoji_in("\n".join(status_notes(ICONS="false"))), [])
        self.assertTrue(all("<img" not in note for note in status_notes(ICONS="false")))

    def test_no_tracked_file_contains_a_literal_emoji(self):
        """Sources, docs and the icons stay ASCII-safe: no emoji anywhere."""
        # Paths are compared relative to the repository root: the checkout itself
        # may well sit under a dotted directory.
        skip = {".git", ".dev", ".claude", "node_modules"}
        offenders = {}
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if not path.is_file() or skip & set(relative.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            found = emoji_in(text)
            if found:
                offenders[str(relative)] = found
        self.assertEqual(offenders, {})
        self.assertGreater(len(list(ROOT.glob("scripts/*.py"))), 5, "the scan found nothing to scan")

    def test_every_script_is_ascii_source(self):
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            body = path.read_bytes()
            self.assertTrue(all(byte < 128 for byte in body), f"{path.name} has non-ASCII bytes")


class TableLayoutTest(unittest.TestCase):
    """The rendered table is well formed in both modes."""

    def columns(self, line):
        return len(line.strip().strip("|").split("|"))

    def test_row_widths_match_the_header(self):
        for graphics in (True, False):
            env = {"GITHUB_REPOSITORY": "o/r", "HEAD_SHA": SHA, "ICONS": "true" if graphics else "false"}
            ctx = pr.Context(env)
            for inline_ids in (None, set()):
                rows = pr.issues_table([finding(1), finding(2, start=22)], ctx, inline_ids).splitlines()
                expected = 3 + (1 if graphics else 0) + (0 if inline_ids is None else 1)
                self.assertEqual({self.columns(row) for row in rows}, {expected},
                                 f"graphics={graphics} inline_ids={inline_ids}: {rows}")


if __name__ == "__main__":
    unittest.main()

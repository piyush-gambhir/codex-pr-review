#!/usr/bin/env python3
"""Regenerate `icons/` from the Octicons npm package.

    npm pack @primer/octicons@19.38.0
    tar -xzf primer-octicons-19.38.0.tgz
    python3 scripts/dev/build_icons.py package/build/svg

Each source glyph is copied with an explicit `fill` for what it means in a
review, stripped down to one line, and written under the name
`scripts/icons.py` asks for. The colours are checked against GitHub's light
(#ffffff) and dark (#0d1117) comment backgrounds and the script fails if any of
them drops below MIN_CONTRAST on either, so a recolour can never ship illegible.

Development only; nothing the action runs imports this. Standard library only.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import icons as catalogue  # noqa: E402

LIGHT_BG = "#ffffff"
DARK_BG = "#0d1117"
MIN_CONTRAST = 3.0

# Semantic name in scripts/icons.py -> (Octicon, fill).
#
# P0 to P3 are four different glyphs, not one glyph in four colours, so the
# severity survives greyscale and colour blindness; the P-number is next to it
# in every layout anyway. Everything procedural (where a finding was reported,
# what a status note is about) is the one neutral grey.
RECIPE = {
    "p0": ("alert-fill-16", "#da3633"),
    "p1": ("alert-16", "#ca5c14"),
    "p2": ("issue-opened-16", "#a67500"),
    "p3": ("info-16", "#1f6feb"),
    "resolved": ("check-circle-fill-16", "#1f883d"),
    "suggestion": ("light-bulb-16", "#8957e5"),
    "inline": ("comment-16", "#6e7781"),
    "outside": ("file-diff-16", "#6e7781"),
    "still-open": ("issue-reopened-16", "#6e7781"),
    "in-progress": ("sync-16", "#1f6feb"),
    "failed": ("x-circle-fill-16", "#da3633"),
    "skipped": ("skip-16", "#6e7781"),
}


# Contrast ---------------------------------------------------------------------


def luminance(colour: str) -> float:
    """WCAG 2.1 relative luminance of an #rrggbb colour."""
    digits = colour.lstrip("#")
    parts = []
    for index in (0, 2, 4):
        value = int(digits[index:index + 2], 16) / 255.0
        parts.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(one: str, other: str) -> float:
    first, second = luminance(one), luminance(other)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


# Building ---------------------------------------------------------------------


def recolour(svg: str, fill: str) -> str:
    """The glyph with one explicit fill on the root element, on a single line."""
    svg = re.sub(r"<\?xml.*?\?>", "", svg, flags=re.DOTALL)
    svg = re.sub(r"\s+", " ", svg).strip()
    if 'fill="' in svg.split(">", 1)[0]:
        raise SystemExit("::error::%s already sets a fill on <svg>." % fill)
    return svg.replace("<svg ", '<svg fill="%s" ' % fill, 1) + "\n"


def main(argv: list) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    source = pathlib.Path(argv[0])
    if not source.is_dir():
        print("::error::%s is not a directory of Octicon SVGs." % source)
        return 1
    out = pathlib.Path(__file__).resolve().parents[2] / "icons"
    out.mkdir(exist_ok=True)

    failures = []
    for name, (octicon, fill) in sorted(RECIPE.items()):
        light, dark = contrast(fill, LIGHT_BG), contrast(fill, DARK_BG)
        if min(light, dark) < MIN_CONTRAST:
            failures.append("%s (%s): light %.2f:1, dark %.2f:1" % (name, fill, light, dark))
        svg = source / ("%s.svg" % octicon)
        if not svg.is_file():
            failures.append("%s: %s not found in %s" % (name, svg.name, source))
            continue
        target = out / catalogue.CATALOGUE[name][0]
        target.write_text(recolour(svg.read_text(encoding="utf-8"), fill), encoding="utf-8")
        print("%-12s %-22s %s  light %.2f:1  dark %.2f:1  %d bytes"
              % (name, octicon, fill, light, dark, target.stat().st_size))

    missing = sorted(set(catalogue.CATALOGUE) - set(RECIPE))
    if missing:
        failures.append("no recipe for %s" % ", ".join(missing))
    licence = source.parent.parent / "LICENSE"
    if licence.is_file():
        shutil.copyfile(str(licence), str(out / "LICENSE"))
        print("copied %s -> icons/LICENSE" % licence)
    for failure in failures:
        print("::error::%s" % failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

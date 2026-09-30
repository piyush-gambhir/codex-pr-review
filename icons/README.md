# Icons

The 16px glyphs the posted review, the status notes, the check run summary and
the job summary use instead of emoji. They are [Octicons](https://primer.style/octicons/)
19.38.0 (MIT, see [`LICENSE`](LICENSE)), copied with one explicit `fill` for
what each one means in a review.

GitHub proxies images in comments through camo, so the action references them by
URL rather than inlining them. The default URL is jsDelivr for this repository
at the ref the action was resolved from, or at the pinned ref in
[`scripts/icons.py`](../scripts/icons.py) when that ref cannot be trusted to be
public and immutable:

```
https://cdn.jsdelivr.net/gh/piyush-gambhir/codex-pr-review@<ref>/icons/<file>.svg
```

Point the `icon-base-url` input somewhere else to serve them from a mirror, or
set `icons: false` to render every layout as words with no external images at
all.

| File | Octicon | Fill | Light #ffffff | Dark #0d1117 | Means |
|---|---|---|---|---|---|
| `priority-p0.svg` | `alert-fill` | `#da3633` | 4.61:1 | 4.11:1 | P0, critical |
| `priority-p1.svg` | `alert` | `#ca5c14` | 4.16:1 | 4.55:1 | P1, high |
| `priority-p2.svg` | `issue-opened` | `#a67500` | 4.07:1 | 4.66:1 | P2, medium |
| `priority-p3.svg` | `info` | `#1f6feb` | 4.63:1 | 4.08:1 | P3, low |
| `resolved.svg` | `check-circle-fill` | `#1f883d` | 4.52:1 | 4.19:1 | fixed since the last review |
| `suggestion.svg` | `light-bulb` | `#8957e5` | 4.61:1 | 4.11:1 | carries a suggested fix |
| `inline.svg` | `comment` | `#6e7781` | 4.55:1 | 4.16:1 | commented inline on the diff |
| `outside.svg` | `file-diff` | `#6e7781` | 4.55:1 | 4.16:1 | reported in the review body |
| `still-open.svg` | `issue-reopened` | `#6e7781` | 4.55:1 | 4.16:1 | reported again |
| `in-progress.svg` | `sync` | `#1f6feb` | 4.63:1 | 4.08:1 | review running |
| `failed.svg` | `x-circle-fill` | `#da3633` | 4.61:1 | 4.11:1 | review failed |
| `skipped.svg` | `skip` | `#6e7781` | 4.55:1 | 4.16:1 | review skipped |

P0 to P3 are four different glyphs rather than one glyph in four colours, so the
severity reads without colour; the neutral grey is for anything procedural. The
contrast columns are WCAG 2.1 ratios against GitHub's comment backgrounds, all
of them above the 3:1 minimum for non-text content on both themes, so no
`prefers-color-scheme` switching is needed.

## Regenerating

```bash
cd "$(mktemp -d)"
npm pack @primer/octicons@19.38.0
tar -xzf primer-octicons-19.38.0.tgz
python3 /path/to/codex-pr-review/scripts/dev/build_icons.py package/build/svg
```

[`scripts/dev/build_icons.py`](../scripts/dev/build_icons.py) holds the
glyph-and-colour recipe, refuses to write a colour under 3:1 on either
background, and copies the package's `LICENSE` here. Add or rename an icon in
`CATALOGUE` in [`scripts/icons.py`](../scripts/icons.py) and in `RECIPE` in the
build script together; the test suite fails if the two disagree or if a file is
missing.

#!/usr/bin/env python3
"""The icon set the posted review uses, and where its images are served from.

Everything this action writes to a pull request is plain GitHub Markdown plus a
handful of 16px Octicon SVGs, so a review reads like GitHub's own review UI
rather than like a chat message. There is no emoji anywhere.

The images live in `icons/` in this repository and are served through jsDelivr,
which GitHub then proxies through camo. `icon-base-url` points somewhere else
(a mirror, a GHES-reachable host), and `icons: false` turns images off
completely: every renderer then falls back to words, so a review stays fully
readable with no external requests at all.

Resolving the default base URL, in order:

1. the `icon-base-url` input, when it is set;
2. jsDelivr for this repository at the ref the running action was resolved from,
   but only when that really is this public repository at a tag or a commit SHA;
3. jsDelivr for this repository at PINNED_REF below, which is what a local `./`
   checkout, a private mirror or a branch ref gets, so an image URL can never
   point at a private repository or at a branch that moves under it.

Standard library only, so it runs on any runner without installing anything.
"""

from __future__ import annotations

import re

# The public repository the icons are published from.
ACTION_REPO = "piyush-gambhir/codex-pr-review"
# Fallback ref for the default icon URL. Release checklist: bump this to the tag
# being released whenever `icons/` changes, before tagging (see CHANGELOG.md).
PINNED_REF = "v1.3.0"
CDN = "https://cdn.jsdelivr.net/gh/{repo}@{ref}/icons"
# Refs that are safe to serve from: an immutable tag or a full commit SHA.
TAG_RE = re.compile(r"\Av\d+(?:\.\d+){0,2}\Z")
SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
# Every image is rendered at the size GitHub uses for its own inline octicons.
SIZE = 16

# Semantic name -> (file in icons/, alt text). The alt text is what a screen
# reader and a text-only client see, so it has to say what the icon means rather
# than name the picture.
CATALOGUE = {
    "p0": ("priority-p0.svg", "Critical"),
    "p1": ("priority-p1.svg", "High"),
    "p2": ("priority-p2.svg", "Medium"),
    "p3": ("priority-p3.svg", "Low"),
    "resolved": ("resolved.svg", "Resolved"),
    "suggestion": ("suggestion.svg", "Suggested fix"),
    "inline": ("inline.svg", "Commented inline on the diff"),
    "outside": ("outside.svg", "Reported in the review body"),
    "still-open": ("still-open.svg", "Reported again"),
    "in-progress": ("in-progress.svg", "In progress"),
    "failed": ("failed.svg", "Failed"),
    "skipped": ("skipped.svg", "Skipped"),
}


def priority(value: int) -> str:
    """The catalogue name for a finding's priority, P3 for anything unexpected."""
    name = "p%d" % value
    return name if name in CATALOGUE else "p3"


def resolve_ref(env: dict) -> str:
    """The ref to serve icons from: the running action's own, or the pinned one."""
    if (env.get("GITHUB_ACTION_REPOSITORY") or "").strip().lower() != ACTION_REPO:
        return PINNED_REF
    ref = (env.get("GITHUB_ACTION_REF") or "").strip()
    return ref if TAG_RE.match(ref) or SHA_RE.match(ref) else PINNED_REF


def default_base_url(env: dict) -> str:
    return CDN.format(repo=ACTION_REPO, ref=resolve_ref(env))


class Icons:
    """Renders one icon, or the words that stand in for it when images are off."""

    def __init__(self, base_url: str = "", enabled: bool = True):
        self.base_url = (base_url or "").rstrip("/")
        self.enabled = bool(enabled and self.base_url)

    @classmethod
    def from_env(cls, env: dict) -> "Icons":
        enabled = (env.get("ICONS", "true") or "true").strip().lower() != "false"
        base = (env.get("ICON_BASE_URL", "") or "").strip() or default_base_url(env)
        return cls(base, enabled)

    def img(self, name: str) -> str:
        """`<img>` for one icon, or "" when images are off.

        Explicit width and height keep the image at text size before it loads,
        and the alt text carries the meaning wherever it does not load at all.
        """
        if not self.enabled:
            return ""
        file, alt = CATALOGUE[name]
        return '<img src="%s/%s" width="%d" height="%d" alt="%s">' % (
            self.base_url, file, SIZE, SIZE, alt)

    def tagged(self, name: str, text: str) -> str:
        """`icon text`, or just the text when images are off."""
        image = self.img(name)
        return "%s %s" % (image, text) if image else text

    def marker(self, name: str, text: str) -> str:
        """A trailing marker on a table row: the icon, or a small word instead."""
        return self.img(name) or "<sub>(%s)</sub>" % text

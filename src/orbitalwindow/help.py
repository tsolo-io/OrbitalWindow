# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""User guide pages from ``docs/content/user/*.md`` with the user's details filled in.

Pages are ordered by file name (``01-…``). Only the known placeholders are replaced, by plain
string substitution, so other braces in the text are left alone.
"""

import re
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDERS = ("host", "user", "samba_path", "ssh_cmd", "groups")
COMMENT_RE = re.compile(r"<!--.*?-->\s*", re.S)
SLUG_RE = re.compile(r"^[0-9]{2}-([a-z0-9-]+)\.md$")


@dataclass(frozen=True)
class HelpPage:
    slug: str  # "getting-started"
    title: str
    path: Path


def list_pages(folder: Path) -> list[HelpPage]:
    """List the help pages in a folder, ordered by file name.

    The title is the page's first ``# `` heading, or else made from its slug.

    Args:
        folder: The folder holding the ``NN-<slug>.md`` files.

    Returns:
        The pages; empty if the folder does not exist.
    """
    pages = []
    if not folder.is_dir():
        return pages
    for path in sorted(folder.glob("*.md")):
        match = SLUG_RE.match(path.name)
        if not match:
            continue
        title = next(
            (line[2:].strip() for line in path.read_text().splitlines() if line.startswith("# ")),
            match.group(1).replace("-", " ").capitalize(),
        )
        pages.append(HelpPage(match.group(1), title, path))
    return pages


def render(page: HelpPage, values: dict[str, str]) -> str:
    """Return a page's Markdown with the licence comment removed and placeholders filled in.

    Args:
        page: The help page.
        values: Replacement text by placeholder name; missing ones become empty.

    Returns:
        The Markdown, with links to other pages pointing at ``/help/<slug>``.
    """
    text = COMMENT_RE.sub("", page.path.read_text())
    for key in PLACEHOLDERS:
        text = text.replace("{" + key + "}", values.get(key, ""))
    # Links between pages use file names (so they also work in the docs site); map to /help/<slug>.
    return re.sub(r"\]\(0[0-9]-([a-z0-9-]+)\.md(#[a-z0-9-]+)?\)", r"](/help/\1\2)", text)

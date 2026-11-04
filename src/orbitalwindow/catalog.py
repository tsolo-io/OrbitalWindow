# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Images an admin can add on the Desktops page, with the logo of their OS (#64).

Desktops are LinuxServer webtop tags, read from LinuxServer's image API (kept for a day, with a
built-in list when the API can't be reached). Terminal-only images are a fixed list of LinuxServer
base images. Only the distributions OrbitalWindow supports are listed (ADR 0034).
"""

import re
import threading
import time
from dataclasses import dataclass

import httpx2

API_URL = "https://api.linuxserver.io/api/v1/images?include_config=false&include_deprecated=false"
ICONS = "https://cdn.jsdelivr.net/gh/selfhst/icons@main/png/"
CACHE_SECONDS = 24 * 3600
DISTROS = ("ubuntu", "debian", "fedora", "alpine", "arch")
WEBTOP_TAG_RE = re.compile(rf"(?:{'|'.join(DISTROS)})-[a-z0-9]+")

# The webtop tags on 2026-11-04, for when the API can't be reached.
FALLBACK_WEBTOP = (
    ("alpine-i3", "i3 Alpine"),
    ("alpine-kde", "KDE Alpine"),
    ("alpine-mate", "MATE Alpine"),
    ("arch-i3", "i3 Arch"),
    ("arch-kde", "KDE Arch"),
    ("arch-mate", "MATE Arch"),
    ("arch-xfce", "XFCE Arch"),
    ("debian-i3", "i3 Debian"),
    ("debian-kde", "KDE Debian"),
    ("debian-mate", "MATE Debian"),
    ("debian-xfce", "XFCE Debian"),
    ("fedora-i3", "i3 Fedora"),
    ("fedora-kde", "KDE Fedora"),
    ("fedora-mate", "MATE Fedora"),
    ("fedora-xfce", "XFCE Fedora"),
    ("ubuntu-i3", "i3 Ubuntu"),
    ("ubuntu-kde", "KDE Ubuntu"),
    ("ubuntu-lxqt", "LXQt Ubuntu"),
    ("ubuntu-mate", "MATE Ubuntu"),
    ("ubuntu-xfce", "XFCE Ubuntu"),
)

TERMINAL_IMAGES = (
    ("baseimage-ubuntu:noble", "Ubuntu 24.04 LTS"),
    ("baseimage-ubuntu:jammy", "Ubuntu 22.04 LTS"),
    ("baseimage-debian:trixie", "Debian 13"),
    ("baseimage-debian:bookworm", "Debian 12"),
    ("baseimage-fedora:43", "Fedora 43"),
    ("baseimage-fedora:42", "Fedora 42"),
    ("baseimage-alpine:3.23", "Alpine 3.23"),
    ("baseimage-alpine:3.22", "Alpine 3.22"),
    ("baseimage-arch:latest", "Arch Linux"),
)

# Logos of the distributions (selfh.st icons). Ubuntu flavours that match a desktop come first.
FLAVOUR_ICONS = {"ubuntu-kde": "kubuntu", "ubuntu-xfce": "xubuntu"}
DISTRO_ICONS = {
    "ubuntu": "ubuntu",
    "debian": "debian",
    "fedora": "fedora",
    "alpine": "alpine-linux",
    "arch": "arch-linux",
}


@dataclass(frozen=True)
class CatalogImage:
    """An image that can be added.

    Attributes:
        name: What the admin types: a webtop tag, or a LinuxServer image for terminal only.
        description: For example ``XFCE Ubuntu``.
        gui: True for a desktop, False for terminal only.
        icon: URL of the OS logo.
    """

    name: str
    description: str
    gui: bool
    icon: str


def icon_url(name: str) -> str:
    """Return the logo URL for an image.

    Args:
        name: A webtop tag (``ubuntu-kde``) or a base image (``baseimage-fedora:43``).

    Returns:
        The URL of the flavour's or distribution's logo, or a generic Linux logo.
    """
    name = name.removeprefix("baseimage-")
    if name in FLAVOUR_ICONS:
        return f"{ICONS}{FLAVOUR_ICONS[name]}.png"
    distro = re.split(r"[-:]", name, maxsplit=1)[0]
    return f"{ICONS}{DISTRO_ICONS.get(distro, 'linux')}.png"


def _description(text: str) -> str:
    """Shorten a LinuxServer tag description.

    Args:
        text: For example ``XFCE Ubuntu *Wayland Support``.

    Returns:
        The part before the first ``*`` note, for example ``XFCE Ubuntu``.
    """
    return text.split("*", 1)[0].strip()


def parse_webtop_tags(data: dict) -> list[tuple[str, str]]:
    """Read the webtop tags of supported distributions from LinuxServer's image API.

    Args:
        data: The API's JSON answer.

    Returns:
        ``(tag, description)`` pairs, sorted by tag.

    Raises:
        KeyError: When the answer has no webtop image.
    """
    images = data["data"]["repositories"]["linuxserver"]
    webtop = next(image for image in images if image["name"] == "webtop")
    tags = {
        tag["tag"]: _description(tag.get("desc") or tag["tag"])
        for tag in webtop["tags"]
        if WEBTOP_TAG_RE.fullmatch(tag["tag"])
    }
    if not tags:
        raise KeyError("webtop has no tags")
    return sorted(tags.items())


class Catalog:
    """The images an admin can add, with the webtop tags kept for a day."""

    def __init__(self, client: httpx2.Client | None = None) -> None:
        """Set up the catalog; the API is first read when the list is needed.

        Args:
            client: HTTP client; a new one with a short timeout when None.
        """
        self._client = client or httpx2.Client(timeout=10.0, follow_redirects=True)
        self._lock = threading.Lock()
        self._webtop: list[tuple[str, str]] = []
        self._read_at = 0.0

    def _webtop_tags(self) -> list[tuple[str, str]]:
        """Return the webtop tags, from the API at most once a day.

        Returns:
            ``(tag, description)`` pairs; the built-in list when the API can't be read.
        """
        with self._lock:
            if self._webtop and time.monotonic() - self._read_at < CACHE_SECONDS:
                return self._webtop
            try:
                response = self._client.get(API_URL)
                response.raise_for_status()
                self._webtop = parse_webtop_tags(response.json())
            except httpx2.HTTPError, ValueError, KeyError, TypeError, StopIteration:
                # Try again in an hour, not on every page load.
                self._webtop = self._webtop or list(FALLBACK_WEBTOP)
                self._read_at = time.monotonic() - CACHE_SECONDS + 3600
            else:
                self._read_at = time.monotonic()
            return self._webtop

    def images(self) -> list[CatalogImage]:
        """Return the desktops, then the terminal-only images.

        Returns:
            The images that can be added.
        """
        desktops = [
            CatalogImage(tag, description, True, icon_url(tag))
            for tag, description in self._webtop_tags()
        ]
        terminals = [
            CatalogImage(image, description, False, icon_url(image))
            for image, description in TERMINAL_IMAGES
        ]
        return desktops + terminals

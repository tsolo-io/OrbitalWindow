# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The list of images an admin can add, with OS logos (#64)."""

import httpx2

from orbitalwindow.catalog import FALLBACK_WEBTOP, ICONS, TERMINAL_IMAGES, Catalog, icon_url

API = {
    "status": "OK",
    "data": {
        "repositories": {
            "linuxserver": [
                {"name": "baseimage-x", "tags": [{"tag": "latest", "desc": "x"}]},
                {
                    "name": "webtop",
                    "tags": [
                        {"tag": "latest", "desc": "XFCE Alpine *Wayland Support"},
                        {"tag": "ubuntu-xfce", "desc": "XFCE Ubuntu *Wayland Support"},
                        {"tag": "debian-kde", "desc": "KDE Debian"},
                        {"tag": "el-kde", "desc": "KDE Enterprise Linux"},
                        {"tag": "dev", "desc": "KDE Ubuntu *Current Selkies Development Head"},
                    ],
                },
            ]
        }
    },
}


def client(handler):
    """Make an HTTP client that answers with ``handler``.

    Args:
        handler: Function from a request to a response.

    Returns:
        The client.
    """
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_webtop_tags_from_the_api():
    """Only supported distributions are listed, with the notes cut from the description."""
    calls = []

    def answer(request):
        """Answer like LinuxServer's API.

        Args:
            request: The request.

        Returns:
            The API answer.
        """
        calls.append(request)
        return httpx2.Response(200, json=API)

    catalog = Catalog(client(answer))
    desktops = [(i.name, i.description) for i in catalog.images() if i.gui]
    assert desktops == [("debian-kde", "KDE Debian"), ("ubuntu-xfce", "XFCE Ubuntu")]
    catalog.images()
    assert len(calls) == 1  # kept for a day


def test_built_in_list_when_the_api_is_down():
    """Without the API the built-in webtop tags are listed."""

    def down(request):
        """Fail like a server without internet.

        Args:
            request: The request.

        Raises:
            httpx2.ConnectError: Always.
        """
        raise httpx2.ConnectError("no route", request=request)

    images = Catalog(client(down)).images()
    assert [(i.name, i.description) for i in images if i.gui] == list(FALLBACK_WEBTOP)
    assert [i.name for i in images if not i.gui] == [name for name, _ in TERMINAL_IMAGES]


def test_icons():
    """Ubuntu flavours get their own logo; others get the distribution's or a generic one."""
    assert icon_url("ubuntu-kde") == f"{ICONS}kubuntu.png"
    assert icon_url("ubuntu-xfce") == f"{ICONS}xubuntu.png"
    assert icon_url("ubuntu-mate") == f"{ICONS}ubuntu.png"
    assert icon_url("alpine-i3") == f"{ICONS}alpine-linux.png"
    assert icon_url("baseimage-arch:latest") == f"{ICONS}arch-linux.png"
    assert icon_url("baseimage-fedora:43") == f"{ICONS}fedora.png"
    assert icon_url("other") == f"{ICONS}linux.png"


def test_images_already_added_are_marked(service):
    """Images on the server are marked as added, desktops and terminal-only ones."""
    service.catalog = Catalog(client(lambda request: httpx2.Response(200, json=API)))
    service.add_terminal_image("boss", "baseimage-fedora:43")
    added = {row["name"] for row in service.image_catalog() if row["added"]}
    assert added == {"ubuntu-xfce", "baseimage-fedora:43"}

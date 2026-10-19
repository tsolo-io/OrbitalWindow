# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""What software a user has: add-ons (open, granted, switched on) and their own packages.

Also decides which image a desktop starts from and whether the per-user image is up to date.
"""

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .addons import MANAGERS, Addon, Catalog, load_catalog
from .db import Database
from .users import ValidationError

# Debian names are lower case; Fedora, Alpine and Arch also use upper case, _ and @.
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._@-]{0,127}$")
MAX_PACKAGES = 200


def user_image_ref(name: str) -> str:
    """Return the Docker image reference of a user's per-user image.

    Args:
        name: OrbitalWindow user name.

    Returns:
        ``ow-user-<name>:latest``.
    """
    return f"ow-user-{name}:latest"


@dataclass(frozen=True)
class AddonState:
    """One add-on as a particular user sees it on the dashboard."""

    addon: Addon
    restricted: bool
    granted: bool
    selected: bool
    needs_desktop: bool = False  # a GUI add-on while the user's image has no desktop
    unsupported: str = ""  # the user's system, when the add-on doesn't work on it

    @property
    def available(self) -> bool:
        """Tell whether the user may switch the add-on on.

        Returns:
            True if the add-on is open to everyone or granted to the user.
        """
        return not self.restricted or self.granted

    @property
    def active(self) -> bool:
        """Tell whether the add-on is in use: switched on and available.

        Returns:
            True if the user selected the add-on, may use it, and has a desktop if it needs one.
        """
        return self.selected and self.available and not self.needs_desktop and not self.unsupported


@dataclass(frozen=True)
class UserPlan:
    """Inputs of a per-user image. ``hash`` is None when no per-user image is needed."""

    user_id: int
    name: str
    image_id: int
    base_ref: str
    base_docker_id: str | None
    addons: tuple[Addon, ...]  # active add-ons that change the image, sorted by key
    packages: tuple[str, ...]
    hash: str | None
    manager: str = "apt"  # the image's package manager (ow-pkg)


class Software:
    def __init__(self, db: Database, addons_dir: Path) -> None:
        """Create the software manager and load the add-on catalog.

        Args:
            db: The application database.
            addons_dir: The folder holding the add-on definitions.
        """
        self._db = db
        self._dir = addons_dir
        self._lock = threading.Lock()
        self.catalog: Catalog = Catalog()
        self.reload()

    def reload(self) -> Catalog:
        """Reload the add-on definitions and make sure each has a settings row.

        Returns:
            The new catalog.
        """
        catalog = load_catalog(self._dir)
        with self._lock:
            self.catalog = catalog
        for key in catalog.addons:
            self._db.execute(
                "INSERT INTO addon_settings (key) VALUES (?) ON CONFLICT (key) DO NOTHING", [key]
            )
        return catalog

    # --- add-on settings ---------------------------------------------------------------------

    def _settings(self) -> dict[str, dict[str, Any]]:
        """Return the admin settings of every add-on.

        Returns:
            The ``addon_settings`` rows by add-on key.
        """
        return {r["key"]: r for r in self._db.all("SELECT * FROM addon_settings")}

    def is_restricted(self, addon: Addon, settings: dict[str, Any] | None = None) -> bool:
        """Tell whether an add-on is restricted.

        Restricted per addon.yaml unless an admin overrode it (``settings`` is its row).

        Args:
            addon: The add-on.
            settings: The add-on's ``addon_settings`` row; looked up when None.

        Returns:
            True if the add-on needs a grant.
        """
        if settings is None:
            settings = self._settings().get(addon.key, {})
        override = settings.get("restricted_override")
        return addon.spec.restricted if override is None else bool(override)

    def enabled_addons(self) -> list[Addon]:
        """Return the add-ons an admin has not switched off.

        Returns:
            The enabled add-ons, sorted by key.
        """
        settings = self._settings()
        return [
            addon
            for key, addon in sorted(self.catalog.addons.items())
            if settings.get(key, {}).get("enabled", True)
        ]

    def _keys(self, sql: str, user_id: int) -> set[str]:
        """Return the add-on keys a query selects for a user.

        Args:
            sql: A query selecting a ``key`` column, with the user id as its only parameter.
            user_id: The user's database id.

        Returns:
            The set of keys.
        """
        return {r["key"] for r in self._db.all(sql, [user_id])}

    def addon_states(self, user_id: int) -> list[AddonState]:
        """Return every enabled add-on as the user sees it.

        Args:
            user_id: The user's database id.

        Returns:
            One state per enabled add-on, sorted by key.
        """
        settings = self._settings()
        granted = self._keys("SELECT key FROM addon_grants WHERE user_id = ?", user_id)
        selected = self._keys("SELECT key FROM user_addons WHERE user_id = ?", user_id)
        image = self._db.one(
            "SELECT coalesce(i.gui, true) AS gui, coalesce(i.pkg_manager, 'apt') AS manager "
            "FROM users u LEFT JOIN images i ON i.id = u.image_id WHERE u.id = ?",
            [user_id],
        ) or {"gui": True, "manager": "apt"}
        system = MANAGERS.get(image["manager"], image["manager"])
        return [
            AddonState(addon, self.is_restricted(addon, settings.get(addon.key)),
                       addon.key in granted, addon.key in selected,
                       addon.spec.gui and not image["gui"],
                       "" if addon.spec.supports(image["manager"]) else system)
            for addon in self.enabled_addons()
        ]  # fmt: skip

    def active_addons(self, user_id: int) -> list[Addon]:
        """Return the add-ons the user has switched on and may use.

        Args:
            user_id: The user's database id.

        Returns:
            The active add-ons, sorted by key.
        """
        return [s.addon for s in self.addon_states(user_id) if s.active]

    def set_addon(self, user_id: int, key: str, on: bool) -> None:
        """Switch an add-on on or off for a user.

        Args:
            user_id: The user's database id.
            key: The add-on key.
            on: True to switch it on, False to switch it off.

        Raises:
            ValidationError: If the add-on is not enabled, or is restricted and not granted.
        """
        state = next((s for s in self.addon_states(user_id) if s.addon.key == key), None)
        if state is None:
            raise ValidationError("That software is not available.")
        if on and not state.available:
            raise ValidationError(f"Ask your administrator for access to {state.addon.spec.label}.")
        if on and state.unsupported:
            label = state.addon.spec.label
            raise ValidationError(f"{label} is not available for {state.unsupported}.")
        if on and state.needs_desktop:
            raise ValidationError(f"{state.addon.spec.label} needs a desktop.")
        if on:
            self._db.execute(
                "INSERT INTO user_addons VALUES (?, ?) ON CONFLICT DO NOTHING", [user_id, key]
            )
        else:
            self._db.execute(
                "DELETE FROM user_addons WHERE user_id = ? AND key = ?", [user_id, key]
            )

    # --- packages ----------------------------------------------------------------------------

    def packages(self, user_id: int) -> list[str]:
        """Return the user's own packages.

        Args:
            user_id: The user's database id.

        Returns:
            The package names, sorted.
        """
        rows = self._db.all(
            "SELECT name FROM user_packages WHERE user_id = ? ORDER BY name", [user_id]
        )
        return [r["name"] for r in rows]

    def search_packages(self, image_id: int | None, prefix: str, limit: int = 30) -> list[str]:
        """Find packages of a desktop image whose names start with a prefix.

        Args:
            image_id: The desktop image id, or None.
            prefix: The start of the package name.
            limit: Maximum number of results.

        Returns:
            Matching package names, shortest first; empty for no image or an invalid prefix.
        """
        prefix = prefix.strip()
        if image_id is None or not prefix or not PACKAGE_RE.fullmatch(prefix):
            return []
        rows = self._db.all(
            "SELECT name FROM pkg_index WHERE image_id = ? AND starts_with(lower(name), ?) "
            "ORDER BY length(name), name LIMIT ?",
            [image_id, prefix.lower(), limit],
        )
        return [r["name"] for r in rows]

    def add_package(self, user_id: int, image_id: int | None, name: str) -> str:
        """Add a package to the user's list.

        Args:
            user_id: The user's database id.
            image_id: The user's desktop image id, or None.
            name: The package name.

        Returns:
            The normalised package name.

        Raises:
            ValidationError: If the name is not valid, no desktop is chosen, the package does
                not exist for the desktop (or its package list is not ready) or the user has
                too many packages.
        """
        name = name.strip()
        if not PACKAGE_RE.fullmatch(name):
            raise ValidationError("That is not a valid package name.")
        if image_id is None:
            raise ValidationError("Choose a desktop first.")
        # The exact name, or else the one name that matches without regard to case.
        matches = self._db.all(
            "SELECT name FROM pkg_index WHERE image_id = ? AND lower(name) = lower(?) "
            "ORDER BY name = ? DESC",
            [image_id, name, name],
        )
        known = bool(matches) and (matches[0]["name"] == name or len(matches) == 1)
        if known:
            name = matches[0]["name"]
        if not known:
            if not self._db.scalar("SELECT count(*) FROM pkg_index WHERE image_id = ?", [image_id]):
                raise ValidationError("The package list for this desktop is not ready yet.")
            raise ValidationError(f"There is no package called '{name}' for this desktop.")
        if len(self.packages(user_id)) >= MAX_PACKAGES:
            raise ValidationError(f"You can add at most {MAX_PACKAGES} packages.")
        self._db.execute(
            "INSERT INTO user_packages VALUES (?, ?) ON CONFLICT DO NOTHING", [user_id, name]
        )
        return name

    def remove_package(self, user_id: int, name: str) -> None:
        """Remove a package from the user's list.

        Args:
            user_id: The user's database id.
            name: The package name.
        """
        self._db.execute(
            "DELETE FROM user_packages WHERE user_id = ? AND name = ?", [user_id, name]
        )

    # --- images ------------------------------------------------------------------------------

    def plan(self, user: dict[str, Any]) -> UserPlan:
        """Work out the inputs of the user's per-user image.

        Args:
            user: The user row.

        Returns:
            The plan; its ``hash`` is None when no per-user image is needed.

        Raises:
            ValidationError: If the user has no desktop image.
        """
        image = self._db.one("SELECT * FROM images WHERE id = ?", [user["image_id"]])
        if image is None:
            raise ValidationError("No desktop has been chosen yet.")
        addons = tuple(a for a in self.active_addons(user["id"]) if a.affects_image)
        packages = tuple(self.packages(user["id"]))
        digest = None
        if addons or packages:
            h = hashlib.sha256()
            h.update(f"base:{image['docker_id'] or image['image_ref']}\n".encode())
            for addon in addons:
                h.update(f"addon:{addon.key}:{addon.files_hash}\n".encode())
            h.update(("packages:" + " ".join(packages)).encode())
            digest = h.hexdigest()
        return UserPlan(
            user["id"], user["name"], image["id"], image["image_ref"], image["docker_id"],
            addons, packages, digest, image["pkg_manager"] or "apt",
        )  # fmt: skip

    def user_image(self, user_id: int) -> dict[str, Any] | None:
        """Return the build record of the user's per-user image.

        Args:
            user_id: The user's database id.

        Returns:
            The ``user_image`` row, or None if none was built.
        """
        return self._db.one("SELECT * FROM user_image WHERE user_id = ?", [user_id])

    def up_to_date(self, user: dict[str, Any]) -> bool:
        """Tell whether the user's last good image build matches the current plan.

        Args:
            user: The user row.

        Returns:
            True if no per-user image is needed or the built one is current.

        Raises:
            ValidationError: If the user has no desktop image.
        """
        plan = self.plan(user)
        if plan.hash is None:
            return True
        row = self.user_image(user["id"])
        # pkg_hash/built_at describe the last *good* build; status is that of the latest build.
        return bool(row and row["built_at"] and row["pkg_hash"] == plan.hash)

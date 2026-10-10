# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""What software a user has: add-ons (open, granted, switched on) and their own apt packages.

Also decides which image a desktop starts from and whether the per-user image is up to date.
"""

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .addons import Addon, Catalog, load_catalog
from .db import Database
from .users import ValidationError

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
MAX_PACKAGES = 200


def user_image_ref(name: str) -> str:
    return f"ow-user-{name}:latest"


@dataclass(frozen=True)
class AddonState:
    """One add-on as a particular user sees it on the dashboard."""

    addon: Addon
    restricted: bool
    granted: bool
    selected: bool

    @property
    def available(self) -> bool:
        return not self.restricted or self.granted

    @property
    def active(self) -> bool:
        return self.selected and self.available


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


class Software:
    def __init__(self, db: Database, addons_dir: Path) -> None:
        self._db = db
        self._dir = addons_dir
        self._lock = threading.Lock()
        self.catalog: Catalog = Catalog()
        self.reload()

    def reload(self) -> Catalog:
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
        return {r["key"]: r for r in self._db.all("SELECT * FROM addon_settings")}

    def is_restricted(self, addon: Addon, settings: dict[str, Any] | None = None) -> bool:
        """Restricted per addon.yaml unless an admin overrode it (``settings`` is its row)."""
        if settings is None:
            settings = self._settings().get(addon.key, {})
        override = settings.get("restricted_override")
        return addon.spec.restricted if override is None else bool(override)

    def enabled_addons(self) -> list[Addon]:
        settings = self._settings()
        return [
            a
            for key, a in sorted(self.catalog.addons.items())
            if settings.get(key, {}).get("enabled", True)
        ]

    def _keys(self, sql: str, user_id: int) -> set[str]:
        return {r["key"] for r in self._db.all(sql, [user_id])}

    def addon_states(self, user_id: int) -> list[AddonState]:
        settings = self._settings()
        granted = self._keys("SELECT key FROM addon_grants WHERE user_id = ?", user_id)
        selected = self._keys("SELECT key FROM user_addons WHERE user_id = ?", user_id)
        return [
            AddonState(a, self.is_restricted(a, settings.get(a.key)), a.key in granted,
                       a.key in selected)
            for a in self.enabled_addons()
        ]  # fmt: skip

    def active_addons(self, user_id: int) -> list[Addon]:
        return [s.addon for s in self.addon_states(user_id) if s.active]

    def set_addon(self, user_id: int, key: str, on: bool) -> None:
        state = next((s for s in self.addon_states(user_id) if s.addon.key == key), None)
        if state is None:
            raise ValidationError("That software is not available.")
        if on and not state.available:
            raise ValidationError(f"Ask your administrator for access to {state.addon.spec.label}.")
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
        rows = self._db.all(
            "SELECT name FROM user_packages WHERE user_id = ? ORDER BY name", [user_id]
        )
        return [r["name"] for r in rows]

    def search_packages(self, image_id: int | None, prefix: str, limit: int = 30) -> list[str]:
        prefix = prefix.strip().lower()
        if image_id is None or not prefix or not PACKAGE_RE.fullmatch(prefix):
            return []
        rows = self._db.all(
            "SELECT name FROM pkg_index WHERE image_id = ? AND starts_with(name, ?) "
            "ORDER BY length(name), name LIMIT ?",
            [image_id, prefix, limit],
        )
        return [r["name"] for r in rows]

    def add_package(self, user_id: int, image_id: int | None, name: str) -> str:
        name = name.strip().lower()
        if not PACKAGE_RE.fullmatch(name):
            raise ValidationError("That is not a valid package name.")
        if image_id is None:
            raise ValidationError("Choose a desktop first.")
        known = self._db.scalar(
            "SELECT count(*) FROM pkg_index WHERE image_id = ? AND name = ?", [image_id, name]
        )
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
        self._db.execute(
            "DELETE FROM user_packages WHERE user_id = ? AND name = ?", [user_id, name]
        )

    # --- images ------------------------------------------------------------------------------

    def plan(self, user: dict[str, Any]) -> UserPlan:
        image = self._db.one("SELECT * FROM images WHERE id = ?", [user["image_id"]])
        if image is None:
            raise ValidationError("No desktop has been chosen yet.")
        addons = tuple(a for a in self.active_addons(user["id"]) if a.affects_image)
        packages = tuple(self.packages(user["id"]))
        digest = None
        if addons or packages:
            h = hashlib.sha256()
            h.update(f"base:{image['docker_id'] or image['image_ref']}\n".encode())
            for a in addons:
                h.update(f"addon:{a.key}:{a.files_hash}\n".encode())
            h.update(("packages:" + " ".join(packages)).encode())
            digest = h.hexdigest()
        return UserPlan(
            user["id"], user["name"], image["id"], image["image_ref"], image["docker_id"],
            addons, packages, digest,
        )  # fmt: skip

    def user_image(self, user_id: int) -> dict[str, Any] | None:
        return self._db.one("SELECT * FROM user_image WHERE user_id = ?", [user_id])

    def up_to_date(self, user: dict[str, Any]) -> bool:
        plan = self.plan(user)
        if plan.hash is None:
            return True
        row = self.user_image(user["id"])
        # pkg_hash/built_at describe the last *good* build; status is that of the latest build.
        return bool(row and row["built_at"] and row["pkg_hash"] == plan.hash)

# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Add-ons: optional software bundles defined as folders ``addons/<key>/addon.yaml``.

Definitions are loaded and validated at startup and when an admin presses **Reload add-ons**.
An invalid definition is reported with its error and skipped; it never stops the service.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)

KEY_RE = r"^[a-z][a-z0-9-]{1,31}$"
APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
# Other package managers allow upper case and a few more characters (Fedora: ImageMagick).
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._@-]{0,127}$")
# Package managers ow-pkg knows (ADR 0034), and the systems that use them, for users.
MANAGERS = {
    "apt": "Ubuntu and Debian",
    "apk": "Alpine",
    "dnf": "Fedora",
    "pacman": "Arch",
}
LAUNCHER_RE = re.compile(r"^ow-[a-z0-9-]+\.desktop$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Environment the desktop spec owns; an add-on may not override it.
RESERVED_ENV = frozenset(
    {
        "PUID", "PGID", "TZ", "TITLE", "CUSTOM_USER", "PASSWORD", "SUBFOLDER",
        "PIXELFLUX_WAYLAND", "START_DOCKER", "NVIDIA_DRIVER_CAPABILITIES", "OW_GROUPS",
        "OW_USER_ENV", "OW_GUI",
    }
)  # fmt: skip
# Container paths an add-on mount may not cover.
RESERVED_TARGETS = ("/config", "/scratch", "/shared", "/groups", "/etc", "/usr", "/bin", "/sbin",
                    "/lib", "/proc", "/sys", "/dev", "/var", "/root", "/run", "/tmp")  # fmt: skip
ADDON_FILE = "addon.yaml"


class Mount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str  # relative to $OW_ROOT
    target: str
    read_only: bool = True

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        """Check that the mount source is a relative path inside ``shared/``.

        Args:
            v: The source path, relative to ``$OW_ROOT``.

        Returns:
            The normalised path.

        Raises:
            ValueError: If the path is absolute, contains ``..`` or is outside ``shared/``.
        """
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError("must be a path relative to OW_ROOT, without '..'")
        if p.parts[0] not in ("shared",):
            raise ValueError("must be inside shared/ (for example shared/matlab)")
        return str(p)

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
        """Check that the mount target is an absolute path outside the reserved locations.

        Args:
            v: The container path.

        Returns:
            The normalised path.

        Raises:
            ValueError: If the path is relative, contains ``..``, is ``/`` or lies inside one of
                ``RESERVED_TARGETS``.
        """
        p = PurePosixPath(v)
        if not p.is_absolute() or ".." in p.parts or str(p) == "/":
            raise ValueError("must be an absolute path such as /opt/matlab")
        for reserved in RESERVED_TARGETS:
            if str(p) == reserved or str(p).startswith(reserved + "/"):
                raise ValueError(f"may not be inside {reserved}")
        return str(p)


class AddonSpec(BaseModel):
    """The contents of ``addon.yaml``."""

    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, Field(pattern=KEY_RE)]
    label: Annotated[str, Field(min_length=1, max_length=60)]
    description: Annotated[str, Field(max_length=500)] = ""
    restricted: bool = False
    apt: list[str] = []
    apk: list[str] = []
    dnf: list[str] = []
    pacman: list[str] = []
    # The package managers (systems) the add-on works on; see ``supports``.
    managers: list[str] | None = None
    script: str | None = None
    launchers: list[str] = []
    mounts: list[Mount] = []
    env: dict[str, str] = {}
    gui: bool = False  # only useful with a desktop: not offered for terminal-only containers

    @field_validator("apt")
    @classmethod
    def _apt(cls, v: list[str]) -> list[str]:
        """Check the apt package names.

        Args:
            v: The package names.

        Returns:
            The unchanged list.

        Raises:
            ValueError: If any name is not a valid package name.
        """
        bad = [p for p in v if not APT_RE.fullmatch(p)]
        if bad:
            raise ValueError(f"not valid package names: {', '.join(bad)}")
        return v

    @field_validator("apk", "dnf", "pacman")
    @classmethod
    def _packages(cls, v: list[str]) -> list[str]:
        """Check the package names for apk, dnf and pacman.

        Args:
            v: The package names.

        Returns:
            The unchanged list.

        Raises:
            ValueError: If any name is not a valid package name.
        """
        bad = [p for p in v if not PACKAGE_RE.fullmatch(p)]
        if bad:
            raise ValueError(f"not valid package names: {', '.join(bad)}")
        return v

    @field_validator("managers")
    @classmethod
    def _managers(cls, v: list[str] | None) -> list[str] | None:
        """Check the package manager names.

        Args:
            v: The names, or None.

        Returns:
            The unchanged list.

        Raises:
            ValueError: If a name is not one of ``MANAGERS``.
        """
        bad = [name for name in v or [] if name not in MANAGERS]
        if bad:
            raise ValueError(f"unknown package managers {bad}; use {', '.join(MANAGERS)}")
        return v

    def packages_for(self, manager: str) -> list[str]:
        """Return the packages to install with a package manager.

        Args:
            manager: ``apt``, ``apk``, ``dnf`` or ``pacman``.

        Returns:
            The package names; empty for an unknown manager.
        """
        return list(getattr(self, manager, [])) if manager in MANAGERS else []

    def supports(self, manager: str) -> bool:
        """Tell whether the add-on works on an image with this package manager.

        ``managers`` decides when given. Otherwise an add-on with package lists works where it
        has a list; one with only a script is assumed to be written for Ubuntu and Debian; one
        with neither (mounts, environment, launchers) works everywhere.

        Args:
            manager: ``apt``, ``apk``, ``dnf`` or ``pacman``.

        Returns:
            True if the add-on can be used there.
        """
        if self.managers is not None:
            return manager in self.managers
        listed = [name for name in MANAGERS if getattr(self, name)]
        if listed:
            return manager in listed
        if self.script:
            return manager == "apt"
        return True

    @field_validator("launchers")
    @classmethod
    def _launchers(cls, v: list[str]) -> list[str]:
        """Check the launcher file names.

        Args:
            v: The launcher file names.

        Returns:
            The unchanged list.

        Raises:
            ValueError: If a name does not look like ``ow-<name>.desktop``.
        """
        bad = [name for name in v if not LAUNCHER_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"launcher file names must look like ow-<name>.desktop: {bad}")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        """Check the environment variable names.

        Args:
            v: The environment variables.

        Returns:
            The unchanged mapping.

        Raises:
            ValueError: If a name is not valid or is one of ``RESERVED_ENV``.
        """
        for name in v:
            if not ENV_RE.fullmatch(name):
                raise ValueError(f"not a valid environment variable name: {name}")
            if name in RESERVED_ENV:
                raise ValueError(
                    f"{name} is set by OrbitalWindow and can't be changed by an add-on"
                )
        return v

    @field_validator("script")
    @classmethod
    def _script(cls, v: str | None) -> str | None:
        """Check that the script is a plain file name in the add-on folder.

        Args:
            v: The script file name, or None.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the name contains a path or starts with a dot.
        """
        if v is not None and (
            "/" in v or v.startswith(".") or not re.fullmatch(r"[A-Za-z0-9._-]+", v)
        ):
            raise ValueError("must be a file name in the add-on folder")
        return v

    @model_validator(mode="after")
    def _unique_targets(self) -> AddonSpec:
        """Check that no two mounts share a target.

        Returns:
            The validated spec.

        Raises:
            ValueError: If two mounts have the same target.
        """
        targets = [m.target for m in self.mounts]
        if len(targets) != len(set(targets)):
            raise ValueError("two mounts have the same target")
        return self


@dataclass
class Addon:
    spec: AddonSpec
    path: Path
    files_hash: str  # hash of every file in the folder: a change means a rebuild

    @property
    def key(self) -> str:
        """Return the add-on key.

        Returns:
            The key from ``addon.yaml``.
        """
        return self.spec.key

    @property
    def affects_image(self) -> bool:
        """Tell whether the add-on changes the per-user image.

        Add-ons with packages, a script or launchers are baked into the per-user image.

        Returns:
            True if the add-on has packages, a script or launchers.
        """
        spec = self.spec
        packages = spec.apt or spec.apk or spec.dnf or spec.pacman
        return bool(packages or spec.script or spec.launchers)


@dataclass
class AddonError:
    key: str
    path: Path
    error: str


@dataclass
class Catalog:
    addons: dict[str, Addon] = field(default_factory=dict)
    errors: list[AddonError] = field(default_factory=list)


def _hash_folder(folder: Path) -> str:
    """Hash the names and contents of every file in a folder.

    Args:
        folder: The add-on folder.

    Returns:
        The SHA-256 hex digest.
    """
    h = hashlib.sha256()
    for file in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(str(file.relative_to(folder)).encode() + b"\0")
        h.update(file.read_bytes() + b"\0")
    return h.hexdigest()


def _format_error(error: ValidationError) -> str:
    """Turn a pydantic validation error into one readable line.

    Args:
        error: The validation error.

    Returns:
        ``location: message`` parts joined by ``; ``.
    """
    parts = []
    for err in error.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "addon.yaml"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_addon(folder: Path) -> Addon:
    """Load and validate one add-on folder.

    Args:
        folder: The add-on folder; its name must match the key.

    Returns:
        The loaded add-on.

    Raises:
        ValueError: With a readable message if the definition is missing or not valid.
    """
    file = folder / ADDON_FILE
    if not file.is_file():
        raise ValueError(f"{ADDON_FILE} is missing")
    try:
        data: Any = yaml.safe_load(file.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"{ADDON_FILE} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{ADDON_FILE} must be a mapping")
    try:
        spec = AddonSpec.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_format_error(exc)) from exc
    if spec.key != folder.name:
        raise ValueError(f"key '{spec.key}' must match the folder name '{folder.name}'")
    for name in [spec.script, *spec.launchers]:
        if name and not (folder / name).is_file():
            raise ValueError(f"file '{name}' is missing from the add-on folder")
    return Addon(spec, folder, _hash_folder(folder))


def load_catalog(addons_dir: Path) -> Catalog:
    """Load every add-on folder, collecting the ones that fail instead of stopping.

    Args:
        addons_dir: The folder holding one sub-folder per add-on.

    Returns:
        The valid add-ons by key and the errors of the invalid ones.
    """
    catalog = Catalog()
    if not addons_dir.is_dir():
        log.warning("add-on folder %s does not exist", addons_dir)
        return catalog
    for folder in sorted(p for p in addons_dir.iterdir() if p.is_dir()):
        if folder.name.startswith("."):
            continue
        try:
            addon = load_addon(folder)
        except (ValueError, OSError) as exc:
            catalog.errors.append(AddonError(folder.name, folder, str(exc)))
            log.warning("add-on %s skipped: %s", folder.name, exc)
            continue
        catalog.addons[addon.key] = addon
    return catalog

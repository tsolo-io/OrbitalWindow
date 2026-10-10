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
LAUNCHER_RE = re.compile(r"^ow-[a-z0-9-]+\.desktop$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Environment the desktop spec owns; an add-on may not override it.
RESERVED_ENV = frozenset(
    {
        "PUID", "PGID", "TZ", "TITLE", "CUSTOM_USER", "PASSWORD", "SUBFOLDER",
        "PIXELFLUX_WAYLAND", "START_DOCKER", "NVIDIA_DRIVER_CAPABILITIES", "OW_GROUPS",
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
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError("must be a path relative to OW_ROOT, without '..'")
        if p.parts[0] not in ("shared",):
            raise ValueError("must be inside shared/ (for example shared/matlab)")
        return str(p)

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
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
    script: str | None = None
    launchers: list[str] = []
    mounts: list[Mount] = []
    env: dict[str, str] = {}

    @field_validator("apt")
    @classmethod
    def _apt(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not APT_RE.fullmatch(p)]
        if bad:
            raise ValueError(f"not valid package names: {', '.join(bad)}")
        return v

    @field_validator("launchers")
    @classmethod
    def _launchers(cls, v: list[str]) -> list[str]:
        bad = [name for name in v if not LAUNCHER_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"launcher file names must look like ow-<name>.desktop: {bad}")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not ENV_RE.fullmatch(k):
                raise ValueError(f"not a valid environment variable name: {k}")
            if k in RESERVED_ENV:
                raise ValueError(f"{k} is set by OrbitalWindow and can't be changed by an add-on")
        return v

    @field_validator("script")
    @classmethod
    def _script(cls, v: str | None) -> str | None:
        if v is not None and (
            "/" in v or v.startswith(".") or not re.fullmatch(r"[A-Za-z0-9._-]+", v)
        ):
            raise ValueError("must be a file name in the add-on folder")
        return v

    @model_validator(mode="after")
    def _unique_targets(self) -> AddonSpec:
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
        return self.spec.key

    @property
    def affects_image(self) -> bool:
        """Add-ons with apt packages, a script or launchers are baked into the per-user image."""
        return bool(self.spec.apt or self.spec.script or self.spec.launchers)


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
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(folder)).encode() + b"\0")
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()


def _format_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "addon.yaml"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_addon(folder: Path) -> Addon:
    """Load and validate one add-on folder. Raises ValueError with a readable message."""
    file = folder / ADDON_FILE
    if not file.is_file():
        raise ValueError(f"{ADDON_FILE} is missing")
    try:
        data: Any = yaml.safe_load(file.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"{ADDON_FILE} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{ADDON_FILE} must be a mapping")
    try:
        spec = AddonSpec.model_validate(data)
    except ValidationError as e:
        raise ValueError(_format_error(e)) from e
    if spec.key != folder.name:
        raise ValueError(f"key '{spec.key}' must match the folder name '{folder.name}'")
    for name in [spec.script, *spec.launchers]:
        if name and not (folder / name).is_file():
            raise ValueError(f"file '{name}' is missing from the add-on folder")
    return Addon(spec, folder, _hash_folder(folder))


def load_catalog(addons_dir: Path) -> Catalog:
    catalog = Catalog()
    if not addons_dir.is_dir():
        log.warning("add-on folder %s does not exist", addons_dir)
        return catalog
    for folder in sorted(p for p in addons_dir.iterdir() if p.is_dir()):
        if folder.name.startswith("."):
            continue
        try:
            addon = load_addon(folder)
        except (ValueError, OSError) as e:
            catalog.errors.append(AddonError(folder.name, folder, str(e)))
            log.warning("add-on %s skipped: %s", folder.name, e)
            continue
        catalog.addons[addon.key] = addon
    return catalog

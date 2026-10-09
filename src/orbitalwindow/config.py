# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Service configuration, read from the environment (and ``deploy/.env`` via compose)."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings.

    Runtime knobs an admin can change live in the DB ``settings`` table instead.
    """

    model_config = SettingsConfigDict(env_prefix="OW_", extra="ignore")

    # Host data root. It is bind-mounted into the service at the *same* path, so paths we hand to
    # Docker for desktop mounts are valid on the host too.
    root: Path = Path("/srv/orbitalwindow")
    hostname: str = "localhost"
    tz: str = Field("Etc/UTC", validation_alias=AliasChoices("OW_TZ", "TZ"))
    admin_password: SecretStr | None = Field(
        None, validation_alias=AliasChoices("CONTROL_ADMIN_PASSWORD", "OW_ADMIN_PASSWORD")
    )
    admin_name: str = "admin"

    docker_host: str = Field(
        "tcp://socket-proxy-rw:2375", validation_alias=AliasChoices("OW_DOCKER_HOST", "DOCKER_HOST")
    )
    # Folder holding the desktop image build context (deploy/image), mounted read-only.
    image_context: Path = Path("/opt/orbitalwindow/image")
    default_desktop: str = "ubuntu-xfce"
    webtop_repo: str = "lscr.io/linuxserver/webtop"

    # Names of the infrastructure containers OrbitalWindow talks to or wires up.
    traefik_container: str = "ow-traefik"
    samba_container: str = "ow-samba"
    # Traefik middlewares defined by the compose stack (on the orbitalwindow service labels).
    auth_middleware: str = "ow-auth-desktop@docker"
    slash_middleware: str = "ow-desktop-slash@docker"
    entrypoint: str = "websecure"

    cookie_secure: bool = True
    session_hours: int = 12
    min_password_length: int = 10
    login_max_failures: int = 5
    login_window_seconds: int = 300

    reconcile_seconds: int = 30
    listen_host: str = "0.0.0.0"
    listen_port: int = 8000

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "orbitalwindow.duckdb"

    @property
    def secret_path(self) -> Path:
        return self.state_dir / "secret.key"

    @property
    def users_dir(self) -> Path:
        return self.root / "users"

    @property
    def groups_dir(self) -> Path:
        return self.root / "groups"

    @property
    def shared_dir(self) -> Path:
        return self.root / "shared"

    @property
    def samba_state_dir(self) -> Path:
        return self.state_dir / "samba"


@lru_cache
def get_settings() -> Settings:
    return Settings()

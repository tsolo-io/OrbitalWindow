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
    # Add-on definitions (addons/), mounted read-only.
    addons_dir: Path = Path("/opt/orbitalwindow/addons")
    default_desktop: str = "ubuntu-xfce"
    webtop_repo: str = "lscr.io/linuxserver/webtop"

    # Names of the infrastructure containers OrbitalWindow talks to or wires up.
    traefik_container: str = "ow-traefik"
    samba_container: str = "ow-samba"
    sshpiper_container: str = "ow-sshpiper"
    ssh_port: int = 2222  # the gateway's published port, shown to users
    # User help pages (docs/content/user), copied into the service image.
    help_dir: Path = Path("/opt/orbitalwindow/help")
    # Traefik middlewares defined by the compose stack (on the orbitalwindow service labels).
    auth_middleware: str = "ow-auth-desktop@docker"
    slash_middleware: str = "ow-desktop-slash@docker"
    entrypoint: str = "websecure"

    # cAdvisor serves its UI and API under --url_base_prefix (the path Traefik routes to it).
    cadvisor_url: str = "http://cadvisor:8080/admin/cadvisor"
    # Prometheus node-exporter on the host (installed by the admin), for the server status page.
    node_exporter_url: str = "http://host.docker.internal:9100/metrics"
    # llmfit image for model suggestions; change the tag to follow llmfit releases.
    llmfit_image: str = "ghcr.io/alexsjones/llmfit:1.1.15"

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
        """Return the service state folder.

        Returns:
            ``<root>/state``.
        """
        return self.root / "state"

    @property
    def db_path(self) -> Path:
        """Return the path of the DuckDB database.

        Returns:
            ``<state_dir>/orbitalwindow.duckdb``.
        """
        return self.state_dir / "orbitalwindow.duckdb"

    @property
    def secret_path(self) -> Path:
        """Return the path of the session signing key.

        Returns:
            ``<state_dir>/secret.key``.
        """
        return self.state_dir / "secret.key"

    @property
    def hf_token_path(self) -> Path:
        """Return the file holding the Hugging Face token.

        Returns:
            ``<state_dir>/hf_token``.
        """
        return self.state_dir / "hf_token"

    @property
    def models_dir(self) -> Path:
        """Return the folder downloaded models are kept in.

        Returns:
            ``<root>/shared/models``.
        """
        return self.root / "shared" / "models"

    @property
    def users_dir(self) -> Path:
        """Return the folder holding the users' homes.

        Returns:
            ``<root>/users``.
        """
        return self.root / "users"

    @property
    def groups_dir(self) -> Path:
        """Return the folder holding the group folders.

        Returns:
            ``<root>/groups``.
        """
        return self.root / "groups"

    @property
    def shared_dir(self) -> Path:
        """Return the shared folder (data and scratch).

        Returns:
            ``<root>/shared``.
        """
        return self.root / "shared"

    @property
    def samba_state_dir(self) -> Path:
        """Return the folder for the Samba state (``users.json``).

        Returns:
            ``<state_dir>/samba``.
        """
        return self.state_dir / "samba"


@lru_cache
def get_settings() -> Settings:
    """Return the settings, read from the environment once and then cached.

    Returns:
        The process-level settings.
    """
    return Settings()

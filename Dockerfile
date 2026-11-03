# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# The OrbitalWindow service image. Built by CI (.github/workflows/publish.yaml) and published as
# ghcr.io/tsolo-io/orbitalwindow:<tag>; locally by deploy/compose.yaml.
#
# Everything the service reads at runtime is baked in, so a production server needs only the
# published images and deploy/compose.production.yaml:
#   /opt/orbitalwindow/image   build context of the desktop base image (deploy/image)
#   /opt/orbitalwindow/addons  add-on definitions (addons/)
#   /opt/orbitalwindow/help    user guide pages (docs/content/user)
# The development compose file mounts the checkout's folders over these to try changes live.
FROM python:3.14-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY LICENSES ./LICENSES
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.14-slim
COPY --from=build /install /usr/local
COPY deploy/image /opt/orbitalwindow/image
COPY addons /opt/orbitalwindow/addons
COPY docs/content/user /opt/orbitalwindow/help
# Runs as an unprivileged user. Root-only file work (home folders) is done by short-lived
# helper containers; see orbitalwindow/hostops.py.
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent orbitalwindow
USER 10001
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
LABEL org.opencontainers.image.title="OrbitalWindow" \
      org.opencontainers.image.description="Manage GPU-accelerated research desktops on Docker" \
      org.opencontainers.image.source="https://github.com/tsolo-io/OrbitalWindow" \
      org.opencontainers.image.licenses="Apache-2.0"
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["orbitalwindow"]

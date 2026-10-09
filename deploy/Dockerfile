# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# The OrbitalWindow service. Build context: the repository root (see deploy/compose.yaml).
FROM python:3.14-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY LICENSES ./LICENSES
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.14-slim
COPY --from=build /install /usr/local
# Runs as an unprivileged user. Root-only file work (home folders) is done by short-lived
# helper containers; see orbitalwindow/hostops.py.
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent orbitalwindow
USER 10001
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["orbitalwindow"]

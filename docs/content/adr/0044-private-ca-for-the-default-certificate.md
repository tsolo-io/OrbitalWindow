<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0044 A private CA signs the default certificate

- Status: accepted
- Date: 2026-11-04

## Context
Until a domain is set up, Traefik serves a fixed self-signed certificate for `OW_HOSTNAME`
(ADR 0020). It was made with `openssl req -x509`, which on OpenSSL 3 marks it as a CA
(`basicConstraints CA:TRUE`). OpenSSL-based clients (browsers, curl, Python) accept it once it is
trusted. rustls/webpki-based clients do not: RFC 5280 does not let a CA certificate be the server
(end-entity) certificate. The Zed editor (rustls-platform-verifier) stopped with
`CaUsedAsEndEntity` on `:8443/api`, and other Rust and some Go programs do the same (#74).

## Decision
- **A private CA and a server certificate.** `deploy/traefik/make-cert.sh` (the `certs` service)
  makes a CA once (10 years, `CA:TRUE, pathlen:0`, `keyCertSign`) and with it signs a server
  certificate for `OW_HOSTNAME` (`CA:FALSE`, `serverAuth`, the name or IP as SAN, 825 days, the
  limit macOS accepts). Traefik serves the server certificate followed by the CA.
- **The CA's key has its own volume,** `ow-traefik-ca`, mounted only in the `certs` container.
  Traefik's volume `ow-traefik-certs` gets the CA certificate (`ca.crt`), not its key.
- **Users trust only the CA.** `/orbitalwindow.crt` serves the last certificate of the chain that
  Traefik sends, which is the CA (or the only certificate of an older or real setup), and
  `make export-cert` saves `ca.crt`. A new server certificate (a new `OW_HOSTNAME`, or renewal 30
  days before it expires, on the next `make start` or `make update`) is signed by the same CA, so
  the users' PCs need nothing.
- **Traefik's TLS configuration is in the volume.** `make-cert.sh` writes `tls.yaml` (the default
  certificate) after each new certificate, and Traefik reads it with
  `--providers.file.filename=/certs/tls.yaml`. Traefik watches that file and loads a new
  certificate without a restart; `compose up` runs `certs` again but does not restart a Traefik
  whose configuration did not change. `deploy/traefik/dynamic.yaml` is gone.
- **Existing servers.** `make-cert.sh` replaces a certificate when `ca.crt` is missing, so
  `make update` (or `install.sh` again) fixes them. The installer then tells the admin that PCs
  which trusted the old `orbitalwindow.crt` need the new one.

## Consequences
- Zed and other rustls clients connect when the CA is installed on the PC.
- After the update from an older version, browsers warn once more, and PCs that trusted the old
  certificate must remove it and install the new `orbitalwindow.crt`.
- A server that runs for more than about 2 years without `make start` or `make update` serves an
  expired certificate until one of them runs.
- With Let's Encrypt (ADR 0019) nothing changes: Traefik uses that certificate, and users need no
  file.

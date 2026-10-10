<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0019 SSH gateway, help pages, log viewer and TLS

- Status: accepted
- Date: 2026-10-10

## Context
Phase 5 of the design: SSH/SFTP through sshpiper, user key management, built-in help, Dozzle for
admins, and real TLS once the DNS provider is known. [0013](0013-files-sharing-and-remote-access.md)
recorded the choices; this record covers how they were built.

## Decision
- **SSH add-on** (`addons/ssh`, open, off by default): installs openssh-server and an s6 service
  running sshd on port 2222 with key login only, `AllowUsers abc`, `internal-sftp`, and
  `AuthorizedKeysFile /etc/ow/upstream.pub`. That file is the per-user *upstream* public key,
  bind-mounted read-only at start (so `StrictModes no`: it is owned by the service, not root/abc).
  The desktop's host key is generated on first start.
- **Gateway:** `farmer1992/sshpiperd:full-v1.6.1` (the plain image lacks the docker plugin) with the
  **failtoban** plugin in front of **docker**. It reads containers through the read-only socket
  proxy and routes by labels OrbitalWindow sets on desktops with the add-on:
  `sshpiper.username`, `container_username=abc`, `port=2222`, `network=ow-net-<user>`,
  `authorized_keys` (the user's keys) and `private_key` (the upstream key). `docker_exec_cmd` is
  not used (it would give root shells). OrbitalWindow connects sshpiper to the user's network at
  start; reconcile reconnects it.
  - sshpiper needs an ordinary bridge network of its own (`ow-gateway`) for its published port:
    on internal networks only, port 2222 disappeared as soon as the last desktop stopped.
  - It runs as root in its container only to write its host key volume, with no capabilities and a
    read-only root filesystem.
  - It does not verify the desktop's host key (the plugin passes none). The upstream hop is on the
    user's private network, reachable only by sshpiper and Traefik, so this is accepted.
  - failtoban bans an address for 60 minutes after 5 failed logins (its defaults).
- **Keys:** users paste public keys (ed25519, ecdsa, sk-*, RSA ≥ 3072 bits; a pasted private key is
  refused with a warning). Stored with a label and SHA256 fingerprint (same as `ssh-keygen -l`),
  without the user's comment. The upstream keypair is ed25519, generated with `cryptography`
  (`state/ssh/<user>{,.pub}`, private key mode 600). Key changes apply at the next start; the
  dashboard says so.
- **Help pages:** `docs/content/user/NN-*.md` (also in the docs site), copied into the service image
  and rendered at `/help/<slug>`. Only the known placeholders `{host} {user} {samba_path} {ssh_cmd}
  {groups}` are replaced, by plain substitution. Dashboard cards have "?" links to them.
- **Dozzle v11.3.0** at `/admin/logs/` behind the admin forwardAuth, in `forward-proxy` mode
  (`Remote-User`). Dozzle trusts that header blindly, so it is only on `ow-core`, a network
  desktops never join. It reads logs through the read-only proxy (which now allows `/info`).
- **TLS:** until the DNS provider is known, Traefik's self-signed certificate. Setting
  `OW_ACME_DNS_PROVIDER` (and `OW_ACME_EMAIL`) in `deploy/.env` makes `make start` add
  `deploy/compose.tls.yaml`: Let's Encrypt via DNS-01 for `OW_HOSTNAME` on the websecure entrypoint,
  so no router labels change. Provider credentials go in `deploy/acme.env`, so Traefik doesn't get
  the other secrets.

## Consequences
- SSH works only while the desktop runs; otherwise sshpiper closes the connection.
- `compose.tls.yaml` repeats Traefik's command list (compose replaces lists); keep both in step.
- TLS is untested until a provider and domain exist.

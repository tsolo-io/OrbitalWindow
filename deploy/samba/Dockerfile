# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# Samba for Windows file access. Users and shares come from /etc/ow/users.json, written by
# OrbitalWindow; see ow-sync and ow-passwd.
FROM alpine:3.24
RUN apk add --no-cache samba samba-common-tools jq tini
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh ow-sync ow-passwd /usr/local/bin/
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/ow-sync /usr/local/bin/ow-passwd \
 && touch /etc/samba/ow-shares.conf
EXPOSE 445
HEALTHCHECK --interval=30s --timeout=5s CMD nc -z 127.0.0.1 445
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]

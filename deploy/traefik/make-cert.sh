#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Create Traefik's self-signed default certificate for OW_HOSTNAME once (10 years). Recreated
# only when OW_HOSTNAME changes, so browsers have to accept it only once.
set -eu
host=${OW_HOSTNAME:?}
cd /certs
if [ -s default.crt ] && [ -s default.key ] && [ "$(cat hostname 2>/dev/null)" = "$host" ]; then
    echo "certificate for $host exists"
    exit 0
fi
case "$host" in
    *[!0-9.]*) san="DNS:$host" ;;
    *) san="IP:$host" ;;
esac
apk add --no-cache openssl >/dev/null
openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -keyout default.key.tmp -out default.crt.tmp \
    -subj "/CN=$host/O=OrbitalWindow" -addext "subjectAltName=$san"
chmod 600 default.key.tmp
mv default.key.tmp default.key
mv default.crt.tmp default.crt
echo "$host" > hostname
echo "created a self-signed certificate for $host"

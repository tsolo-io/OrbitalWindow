#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Create Traefik's default certificate for OW_HOSTNAME and its TLS configuration, tls.yaml
# (ADR 0044). A private CA, made once (10 years) and kept in /ca, signs a server certificate
# (CA:FALSE, 825 days). Traefik serves the server certificate followed by the CA. Users' PCs trust
# only the CA (ca.crt, also copied to /certs), so a new server certificate (new OW_HOSTNAME, or
# renewal 30 days before it expires) needs nothing on the PCs. Clients that use rustls (Zed, other
# Rust programs) refuse a certificate that is its own CA, which is what versions before #74 made:
# those are replaced.
#
# Traefik watches tls.yaml: writing it again makes Traefik load the new certificate without a
# restart. A fixed default certificate is needed because without one Traefik makes a new
# self-signed certificate on every configuration change (every desktop start/stop), and browsers
# that accepted the previous one refuse new connections (e.g. the desktop stream's WebSocket).
# OW_HOSTNAME_ALIASES (optional, separated by spaces or commas) adds more names or IP addresses
# to the server certificate, for a server that clients reach at more than one address (for example
# a second network such as a VPN). Changing it makes a new server certificate, signed by the same CA.
# With OW_ACME_DNS_PROVIDER set, Let's Encrypt certificates are used for OW_HOSTNAME instead.
set -eu
host=${OW_HOSTNAME:?}
names=$(echo "$host ${OW_HOSTNAME_ALIASES:-}" | tr ',' ' ' | xargs)
days=825
now=$(date +%s)
cd /certs
if [ -s tls.yaml ] && [ -s default.crt ] && [ -s default.key ] \
    && [ -s ca.crt ] && cmp -s ca.crt /ca/ca.crt \
    && [ "$(cat hostname 2>/dev/null)" = "$names" ] \
    && [ "$(cat expires 2>/dev/null || echo 0)" -gt $((now + 30 * 86400)) ]; then
    echo "certificate for $names exists"
    exit 0
fi
san=
for name in $names; do
    case "$name" in
        *[!0-9.]*) san="$san,DNS:$name" ;;
        *) san="$san,IP:$name" ;;
    esac
done
san=${san#,}
apk add --no-cache openssl >/dev/null
if ! [ -s /ca/ca.crt ] || ! [ -s /ca/ca.key ]; then
    openssl req -x509 -quiet -newkey rsa:3072 -sha256 -days 3650 -nodes \
        -keyout /ca/ca.key.tmp -out /ca/ca.crt.tmp \
        -subj "/CN=OrbitalWindow CA $host/O=OrbitalWindow" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
    chmod 600 /ca/ca.key.tmp
    mv /ca/ca.key.tmp /ca/ca.key
    mv /ca/ca.crt.tmp /ca/ca.crt
    echo "created the OrbitalWindow CA"
fi
openssl req -quiet -newkey rsa:3072 -sha256 -nodes -keyout default.key.tmp -out server.csr \
    -subj "/CN=$host/O=OrbitalWindow"
printf '%s\n' "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" \
    "extendedKeyUsage=serverAuth" "subjectAltName=$san" > server.ext
openssl x509 -req -sha256 -days "$days" -in server.csr -CA /ca/ca.crt -CAkey /ca/ca.key \
    -set_serial "0x$(openssl rand -hex 16)" -extfile server.ext -out server.crt
cat server.crt /ca/ca.crt > default.crt.tmp
rm server.csr server.ext server.crt
chmod 600 default.key.tmp
mv default.key.tmp default.key
mv default.crt.tmp default.crt
cp /ca/ca.crt ca.crt
echo "$names" > hostname
echo $((now + days * 86400)) > expires
printf '%s\n' "tls:" "  stores:" "    default:" "      defaultCertificate:" \
    "        certFile: /certs/default.crt" "        keyFile: /certs/default.key" > tls.yaml.tmp
mv tls.yaml.tmp tls.yaml
echo "created a certificate for $names, signed by the OrbitalWindow CA"

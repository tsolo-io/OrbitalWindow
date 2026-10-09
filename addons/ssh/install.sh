#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# sshd for the OrbitalWindow SSH gateway (sshpiper). Key login only, as abc, on port 2222.
# The only accepted key is the per-user upstream key OrbitalWindow mounts at start at
# /etc/ow/upstream.pub (read-only); users' own keys are checked by the gateway.
set -eu

mkdir -p /etc/ow /etc/ow/ssh
cat > /etc/ow/sshd_config <<'CONF'
Port 2222
ListenAddress 0.0.0.0
HostKey /etc/ow/ssh/ssh_host_ed25519_key
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers abc
AuthorizedKeysFile /etc/ow/upstream.pub
# The key file is a read-only bind mount owned by the service, not by root or abc.
StrictModes no
X11Forwarding no
AllowAgentForwarding yes
AllowTcpForwarding yes
PrintMotd no
UsePAM no
Subsystem sftp internal-sftp
CONF

# s6-overlay v3 service (the webtop init system).
svc=/etc/s6-overlay/s6-rc.d/svc-ow-sshd
mkdir -p "$svc/dependencies.d"
echo longrun > "$svc/type"
touch "$svc/dependencies.d/init-services"
cat > "$svc/run" <<'RUN'
#!/command/with-contenv bash
# Host key per desktop, kept in the container (recreated on rebuild; sshpiper does not pin it).
[ -f /etc/ow/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N "" -f /etc/ow/ssh/ssh_host_ed25519_key
mkdir -p /run/sshd
exec /usr/sbin/sshd -D -e -f /etc/ow/sshd_config
RUN
chmod 755 "$svc/run"
touch /etc/s6-overlay/s6-rc.d/user/contents.d/svc-ow-sshd

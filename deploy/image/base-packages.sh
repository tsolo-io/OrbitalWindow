#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Installs OrbitalWindow's base tools into a desktop or terminal-only image, on any distribution
# ow-pkg supports (ADR 0034). Run during the image build: base-packages.sh desktop|terminal
# Required tools must install; extras (GPU tools, fonts) are skipped where a distribution lacks them.
set -eu
kind=${1:?desktop or terminal}

common="bash tmux sudo git curl nano less htop rsync unzip ca-certificates"
case "$(ow-pkg manager)" in
    apt)
        required="$common python3 python3-venv procps libcap2-bin"
        extras="nvtop"
        gui="mesa-utils vulkan-tools fonts-dejavu fonts-liberation fonts-noto-core" ;;
    apk)
        # shadow: usermod and groupadd (the init hook); coreutils: install.
        required="$common python3 py3-pip procps-ng libcap shadow coreutils"
        extras="nvtop"
        gui="mesa-utils vulkan-tools font-dejavu font-liberation font-noto" ;;
    dnf)
        required="$common python3 procps-ng libcap shadow-utils"
        extras="nvtop"
        gui="mesa-demos vulkan-tools dejavu-sans-fonts liberation-fonts google-noto-sans-fonts" ;;
    pacman)
        required="$common python procps-ng libcap shadow"
        extras="nvtop"
        gui="mesa-utils vulkan-tools ttf-dejavu ttf-liberation noto-fonts" ;;
    *)
        echo "This image has no supported package manager (apt, apk, dnf or pacman)." >&2
        exit 1 ;;
esac
[ "$kind" = desktop ] && extras="$extras $gui"

# shellcheck disable=SC2086  # word splitting of the lists is intended
ow-pkg install $required
# shellcheck disable=SC2086
ow-pkg install-any $extras

# Ubuntu's nvtop carries cap_perfmon, which is outside Docker's bounding set, so exec fails;
# drop the capability (NVIDIA stats via NVML don't need it).
nvtop=$(command -v nvtop || true)
if [ -n "$nvtop" ] && command -v setcap >/dev/null; then
    setcap -r "$nvtop" 2>/dev/null || true
fi

if [ "$kind" = terminal ]; then
    # abc gets bash as its login shell (the base images give it /bin/false), for SSH and tmux,
    # and its password "!" (locked) becomes "*" (no password, not locked): sshd runs without PAM
    # and refuses locked accounts even for key logins. Desktops let abc use sudo; so do these.
    usermod -s /bin/bash -p '*' abc
    echo 'abc ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/abc
    chmod 440 /etc/sudoers.d/abc
fi

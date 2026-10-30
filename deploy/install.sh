#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# Install or update OrbitalWindow on this server:
#
#   curl -fsSLO https://github.com/tsolo-io/OrbitalWindow/releases/latest/download/install.sh
#   sudo bash install.sh
#
# Docker, Docker Compose and the GPU driver (on NVIDIA also the NVIDIA Container Toolkit) must be
# installed first; the admin guide shows how. The script checks them and says what is missing. A
# server without a GPU (or without a working driver) is installed with desktops that render in
# software.
#
# Run it again to update: it keeps .env and the data folder, replaces the deploy files with those
# of the new version and pulls the new images.
#
# Options (or the environment variable in brackets):
#   --version TAG    release to install, or main (OW_VERSION; default: the latest release)
#   --dir DIR        folder for the deploy files (OW_INSTALL_DIR; default /opt/orbitalwindow)
#   --data DIR       data folder: homes, groups, state (OW_ROOT; default /srv/orbitalwindow)
#   --hostname NAME  name or IP users type (OW_HOSTNAME; default this server's IP address)
#   --gpu VENDOR     nvidia, amd, intel or none (OW_GPU_VENDOR; default: detected)
#   --yes            don't ask questions; use the defaults and the options given
set -euo pipefail

REPO=tsolo-io/OrbitalWindow
PORTS=(80 443 445 2222 8443)

VERSION=${OW_VERSION:-}
INSTALL_DIR=${OW_INSTALL_DIR:-/opt/orbitalwindow}
DATA_DIR=${OW_ROOT:-/srv/orbitalwindow}
HOST_NAME=${OW_HOSTNAME:-}
GPU=${OW_GPU_VENDOR:-}
ASSUME_YES=0

if [[ -t 1 ]]; then
  BOLD=$'\e[1m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RESET=$'\e[0m'
else
  BOLD='' RED='' GREEN='' YELLOW='' RESET=''
fi

# Print a step heading.
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
# Print a check that passed.
ok() { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
# Print a warning; the installation continues.
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
# Print an error and stop.
die() {
  printf '\n%sError:%s %s\n' "$RED" "$RESET" "$1" >&2
  shift
  local line
  for line in "$@"; do printf '       %s\n' "$line" >&2; done
  exit 1
}

# Ask a question with a default answer; prints the answer. Reads the terminal, so it also works
# when the script is piped into bash. Without a terminal, or with --yes, it takes the default.
ask() {
  local question=$1 default=$2 answer=''
  if [[ $ASSUME_YES == 0 ]] && { : </dev/tty; } 2>/dev/null; then
    read -r -p "  $question [$default]: " answer </dev/tty || true
  fi
  printf '%s' "${answer:-$default}"
}

# Ask a yes/no question (default no); succeeds on yes.
confirm() {
  [[ $(ask "$1 (y/N)" n) =~ ^[Yy] ]]
}

# Set KEY=value in the .env file, replacing the line if it is there and adding it if not.
set_env() {
  local key=$1 value=$2 file=$INSTALL_DIR/.env
  if grep -q "^$key=" "$file"; then
    sed -i "s|^$key=.*|$key=$value|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

# Read KEY from the .env file (empty if it is not set).
get_env() {
  sed -n "s/^$1=//p" "$INSTALL_DIR/.env" 2>/dev/null | tail -1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --version) VERSION=${2:?--version needs a value}; shift 2 ;;
    --dir) INSTALL_DIR=${2:?--dir needs a value}; shift 2 ;;
    --data) DATA_DIR=${2:?--data needs a value}; shift 2 ;;
    --hostname) HOST_NAME=${2:?--hostname needs a value}; shift 2 ;;
    --gpu) GPU=${2:?--gpu needs a value}; shift 2 ;;
    --yes | -y) ASSUME_YES=1; shift ;;
    --help | -h) sed -n '6,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1" "Run 'bash install.sh --help' to see the options." ;;
  esac
done
case $GPU in
  '' | nvidia | amd | intel | none) ;;
  *) die "--gpu must be nvidia, amd, intel or none, not '$GPU'." ;;
esac

printf '%sOrbitalWindow installer%s\n' "$BOLD" "$RESET"

step "Checking this server"
[[ $(id -u) == 0 ]] || die "Run the installer as root:" "sudo bash install.sh"
[[ $(uname -m) == x86_64 ]] || die "OrbitalWindow runs on 64-bit Intel or AMD processors (x86_64), not $(uname -m)."
if [[ -r /etc/os-release ]]; then
  # shellcheck source=/dev/null
  os_name=$(. /etc/os-release && printf '%s %s' "$NAME" "$VERSION_ID")
  if [[ $os_name =~ ^Ubuntu\ (24|26)\.04$ ]]; then
    ok "$os_name"
  else
    warn "$os_name: OrbitalWindow is tested on Ubuntu 24.04 and 26.04; continuing anyway."
  fi
fi
for tool in curl tar; do
  command -v "$tool" >/dev/null || die "'$tool' is missing." "Install it: sudo apt install $tool"
done

command -v docker >/dev/null || die "Docker is not installed." \
  "Install it: sudo apt install docker.io docker-compose-v2" \
  "Then run this installer again."
docker info >/dev/null 2>&1 || die "Docker is installed but not running." \
  "Start it: sudo systemctl enable --now docker"
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
docker compose version >/dev/null 2>&1 || die "Docker Compose (v2) is not installed." \
  "Install it: sudo apt install docker-compose-v2"
ok "Docker Compose $(docker compose version --short)"

if command -v make >/dev/null; then
  ok "make"
elif command -v apt-get >/dev/null; then
  warn "make is missing (the day-to-day commands use it); installing it."
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq make >/dev/null || die "Could not install make." \
    "Install it yourself: sudo apt install make"
  ok "make"
else
  die "'make' is missing." "Install it with your package manager, then run this installer again."
fi

update=0
[[ -f $INSTALL_DIR/.env ]] && update=1

step "Looking for GPUs"
# Display controllers (PCI class 0x03…) by vendor, and whether a kernel driver is bound to them.
declare -A gpu_found=() gpu_driver=()
for device in /sys/bus/pci/devices/*; do
  [[ $(cat "$device/class" 2>/dev/null) == 0x03* ]] || continue
  case $(cat "$device/vendor") in
    0x10de) vendor=nvidia ;;
    0x1002) vendor=amd ;;
    0x8086) vendor=intel ;;
    *) continue ;; # e.g. the server's own management VGA chip
  esac
  gpu_found[$vendor]=1
  if [[ -e $device/driver ]]; then
    gpu_driver[$vendor]=$(basename "$(readlink -f "$device/driver")")
  fi
done

# Succeeds when VENDOR's GPU can be used by containers; warns about what is missing.
gpu_usable() {
  local vendor=$1 driver=${gpu_driver[$1]:-}
  case $vendor in
    nvidia)
      if [[ $driver != nvidia ]]; then
        warn "NVIDIA GPU found, but the NVIDIA driver is not loaded${driver:+ (driver in use: $driver)}."
        warn "  Install it: sudo ubuntu-drivers install --gpgpu && sudo reboot"
        return 1
      fi
      if ! nvidia-smi >/dev/null 2>&1; then
        warn "NVIDIA driver loaded, but nvidia-smi does not work (reboot after a driver install?)."
        return 1
      fi
      if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia \
        && ! command -v nvidia-container-runtime-hook >/dev/null; then
        warn "NVIDIA driver works, but the NVIDIA Container Toolkit is missing, so containers can't use the GPU."
        warn "  See 'Preparing an NVIDIA host' in the admin guide."
        return 1
      fi
      ok "NVIDIA GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | paste -sd, -), driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
      ;;
    amd)
      if [[ $driver != amdgpu ]]; then
        warn "AMD GPU found, but the amdgpu driver is not loaded${driver:+ (driver in use: $driver)}."
        return 1
      fi
      ok "AMD GPU (amdgpu driver)"
      ;;
    intel)
      if [[ $driver != i915 && $driver != xe ]]; then
        warn "Intel GPU found, but the i915 or xe driver is not loaded${driver:+ (driver in use: $driver)}."
        return 1
      fi
      ok "Intel GPU ($driver driver)"
      ;;
  esac
  [[ -d /dev/dri ]] || { warn "No /dev/dri on this server, so desktops can't use the GPU."; return 1; }
}

detected=none
for vendor in nvidia amd intel; do # a discrete GPU wins over Intel graphics built into the processor
  if [[ -n ${gpu_found[$vendor]:-} ]] && gpu_usable "$vendor"; then
    detected=$vendor
    break
  fi
done
[[ ${#gpu_found[@]} == 0 ]] && warn "No GPU found."
if [[ $update == 1 ]]; then
  GPU=$(get_env OW_GPU_VENDOR)
  ok "Keeping the GPU setting of this installation: ${GPU:-nvidia}"
  [[ ${GPU:-nvidia} != "$detected" ]] && warn "The check found: $detected. To change it, see 'Changing the GPU later' in the admin guide."
elif [[ -z $GPU ]]; then
  GPU=$detected
elif [[ $GPU != "$detected" ]]; then
  warn "Using --gpu $GPU, although the check found: $detected."
fi
if [[ $update == 0 && $GPU == none ]]; then
  warn "Installing without a GPU: desktops render in software (slower, no GPU computing)."
  warn "  Once the GPU works, set OW_GPU_VENDOR in $INSTALL_DIR/.env, run 'make update' there"
  warn "  and choose the GPU in OrbitalWindow under Settings → GPU."
fi

step "Choosing the version"
if [[ -z $VERSION ]]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1) || true
  if [[ -z $VERSION ]]; then
    VERSION=main
    warn "No release found; installing the latest development version (main)."
  fi
fi
ok "OrbitalWindow $VERSION"

if [[ $update == 1 ]]; then
  ok "Existing installation in $INSTALL_DIR: updating it (settings and data are kept)."
else
  step "Settings"
  default_host=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
  default_host=${default_host:-$(hostname -I | awk '{print $1}')}
  HOST_NAME=${HOST_NAME:-$(ask "Name or IP address users type in the browser" "$default_host")}
  DATA_DIR=$(ask "Data folder (homes, groups, settings; back it up)" "$DATA_DIR")
  time_zone=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)
  time_zone=$(ask "Time zone" "${time_zone:-Etc/UTC}")
  [[ -n $HOST_NAME ]] || die "The host name can't be empty." "Run the installer again with --hostname <name or IP>."

  # Anything else on the ports the stack publishes would stop it from starting.
  busy=()
  for port in "${PORTS[@]}"; do
    [[ -n $(ss -Hltn "sport = :$port" 2>/dev/null) ]] && busy+=("$port")
  done
  if [[ ${#busy[@]} -gt 0 ]]; then
    hint=()
    [[ " ${busy[*]} " == *" 445 "* ]] && systemctl is-active -q smbd 2>/dev/null \
      && hint=("Port 445 is used by this server's own Samba. Turn it off with:" \
        "  sudo systemctl disable --now smbd nmbd")
    die "These ports are already in use: ${busy[*]}." \
      "OrbitalWindow needs ports ${PORTS[*]}. See what uses them with: sudo ss -ltnp" "${hint[@]}"
  fi
  ok "Ports ${PORTS[*]} are free"
fi

step "Downloading OrbitalWindow $VERSION"
download=$(mktemp -d)
trap 'rm -rf "$download"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$VERSION" \
  | tar -xz -C "$download" --strip-components=1 --wildcards '*/deploy/' \
  || die "Could not download version $VERSION from GitHub." \
    "Check the version name and this server's internet access."
mkdir -p "$INSTALL_DIR"
# Replaces the files that come with OrbitalWindow; .env, acme.env and other own files stay.
cp -a "$download/deploy/." "$INSTALL_DIR/"
ok "Deploy files in $INSTALL_DIR"

if [[ $update == 0 ]]; then
  (umask 077 && cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env")
  admin_password=$(head -c 18 /dev/urandom | base64 | tr '+/' '-_')
  set_env CONTROL_ADMIN_PASSWORD "$admin_password"
  set_env OW_HOSTNAME "$HOST_NAME"
  set_env OW_ROOT "$DATA_DIR"
  set_env TZ "$time_zone"
  set_env OW_GPU_VENDOR "$GPU"
  # vLLM needs NVIDIA; llama.cpp runs on the others.
  ai_engine=llamacpp
  [[ $GPU == nvidia ]] && ai_engine=vllm
  set_env OW_AI_ENGINE "$ai_engine"
  compose_file=compose.yaml
  [[ $GPU == nvidia ]] && compose_file+=:compose.nvidia.yaml
  [[ $GPU == intel ]] && compose_file+=:compose.intel.yaml
  set_env COMPOSE_FILE "$compose_file"
  ok "Settings in $INSTALL_DIR/.env"
fi
set_env OW_VERSION "$VERSION"

# The admin who ran sudo owns the folder and gets Docker access, so 'make …' works without sudo.
if [[ -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
  chown -R "$SUDO_USER": "$INSTALL_DIR"
  if ! id -nG "$SUDO_USER" | grep -qw docker; then
    usermod -aG docker "$SUDO_USER"
    warn "Added $SUDO_USER to the docker group: log out and in again before running 'make' commands."
  fi
fi

if [[ $update == 1 ]]; then
  step "Updating OrbitalWindow"
  make -C "$INSTALL_DIR" --no-print-directory update
else
  step "Starting OrbitalWindow (this downloads several GB the first time)"
  make -C "$INSTALL_DIR" --no-print-directory start
fi

printf '  Waiting for the service to be ready'
for _ in $(seq 60); do
  health=$(docker inspect -f '{{.State.Health.Status}}' orbitalwindow 2>/dev/null || true)
  [[ $health == healthy ]] && break
  printf '.'
  sleep 5
done
printf '\n'
[[ $health == healthy ]] || warn "The service is not ready yet. See what it is doing with: cd $INSTALL_DIR && make logs"

if ! curl -fsS -m 3 -o /dev/null http://127.0.0.1:9100/metrics 2>/dev/null; then
  warn "Prometheus node-exporter is not running, so the Server status page stays empty."
  warn "  Install it: sudo apt install prometheus-node-exporter"
fi

step "Done"
url="https://$(get_env OW_HOSTNAME)/"
if [[ $update == 1 ]]; then
  printf '  OrbitalWindow is updated to %s: %s\n' "$VERSION" "$url"
else
  printf '  Open %s%s%s in a browser (accept the certificate warning the first time).\n' "$BOLD" "$url" "$RESET"
  printf '  Log in as %sadmin%s with password %s%s%s and change it on the dashboard.\n' \
    "$BOLD" "$RESET" "$BOLD" "$admin_password" "$RESET"
  printf '  (The password is also in %s/.env as CONTROL_ADMIN_PASSWORD.)\n' "$INSTALL_DIR"
  printf '  The first desktop is built in the background: 10–20 minutes.\n'
fi
printf '  Day-to-day commands: cd %s && make help\n' "$INSTALL_DIR"

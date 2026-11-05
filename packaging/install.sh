#!/usr/bin/env bash
# GLEAPP one-line installer: install or update the latest release.
#
#   curl -fsSL https://raw.githubusercontent.com/abrignoni/GLEAPP/main/packaging/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --user        # no root: ~/.local instead of /opt
#   curl -fsSL .../install.sh | bash -s -- --uninstall  # remove what this installed
#
# What it does, by distribution:
#   Debian/Ubuntu and derivatives -> the release .deb
#   Fedora/RHEL/openSUSE         -> the release .rpm
#   Arch and everything else      -> the release Linux tarball, same layout as
#                                    the AUR gleapp-bin package (/opt/gleapp,
#                                    /usr/bin/gleapp). On Arch you can also use
#                                    an AUR helper instead: yay -S gleapp-bin
#                                    for the release, gleapp-git for every commit.
#
# Every download is checked against the release's SHA256SUMS.txt before it is
# installed. Only Linux x86_64 has release binaries; elsewhere (and for a
# source install anywhere) the README's "Install from source" section applies.
#
# Env: GLEAPP_VERSION pins a version (default: latest release),
#      GLEAPP_REPO overrides the repo (default: abrignoni/GLEAPP).
set -euo pipefail

REPO="${GLEAPP_REPO:-abrignoni/GLEAPP}"
VERSION="${GLEAPP_VERSION:-}"
MODE="auto"      # auto | system | user
UNINSTALL=0

usage() {
  cat <<'EOF'
Usage: install.sh [--user|--system] [--uninstall] [--help]

  --user       install under ~/.local (no root needed)
  --system     install under /opt + /usr/bin (needs root or sudo)
  --uninstall  remove a previous install and exit
  --help       this text

Environment: GLEAPP_VERSION pins a version (default: latest release),
             GLEAPP_REPO overrides the repository.
EOF
}

log() { printf '==> %s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || err "needs '$1' on PATH"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --user) MODE="user" ;;
    --system) MODE="system" ;;
    --uninstall) UNINSTALL=1 ;;
    --help|-h) usage; exit 0 ;;
    *) err "unknown argument '$1' (see --help)" ;;
  esac
  shift
done

have_sudo() { [ "$(id -u)" = 0 ] || command -v sudo >/dev/null 2>&1; }
run_priv() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

os_id() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s %s' "${ID:-}" "${ID_LIKE:-}"
  fi
}

latest_version() {
  if [ -n "$VERSION" ]; then echo "$VERSION"; return 0; fi
  local tag=""
  tag=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | grep '"tag_name"' | head -1 | cut -d'"' -f4 || true)
  if [ -z "$tag" ]; then
    # API rate-limited or unreachable: the /latest page redirects to the tag.
    tag=$(curl -fsSL -o /dev/null -w '%{url_effective}' \
      "https://github.com/$REPO/releases/latest" 2>/dev/null | sed 's#.*/##' || true)
  fi
  [ -n "$tag" ] || err "could not determine the latest release (set GLEAPP_VERSION)"
  echo "$tag"
}

download() { # $1=url $2=dest
  log "downloading $1"
  curl -fsSL --retry 3 -o "$2" "$1"
}

verify() { # $1=dir $2=file (checks it against dir/SHA256SUMS.txt)
  (cd "$1" && grep -F " $2" SHA256SUMS.txt | sha256sum -c - >/dev/null) \
    || err "checksum mismatch for $2"
}

install_tarball() { # $1=tarball path, system-wide when $2=system
  local tarball="$1" scope="$2" root bin desktop
  if [ "$scope" = system ]; then
    root=/opt/gleapp bin=/usr/bin desktop=/usr/share/applications
    run_priv mkdir -p /opt/gleapp
    run_priv tar -xzf "$tarball" -C /opt/gleapp --strip-components=1
    run_priv ln -sf /opt/gleapp/GLEAPP /usr/bin/gleapp
  else
    root="$HOME/.local/share/gleapp" bin="$HOME/.local/bin" desktop="$HOME/.local/share/applications"
    mkdir -p "$root" "$bin" "$desktop"
    tar -xzf "$tarball" -C "$root" --strip-components=1
    ln -sf "$root/GLEAPP" "$bin/gleapp"
  fi
  # Same desktop entry the .deb/.rpm/AUR packages ship.
  if [ "$scope" = system ]; then
    run_priv tee "$desktop/gleapp.desktop" >/dev/null <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=GLEAPP
GenericName=Media forensics triage
Comment=Triage and analyze large sets of images and video in a digital-forensics workflow
Exec=gleapp
Icon=gleapp
Terminal=false
Categories=Utility;
Keywords=forensics;images;video;triage;exif;hash;
StartupWMClass=GLEAPP
DESKTOP
  else
    cp /dev/stdin "$desktop/gleapp.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=GLEAPP
GenericName=Media forensics triage
Comment=Triage and analyze large sets of images and video in a digital-forensics workflow
Exec=gleapp
Icon=gleapp
Terminal=false
Categories=Utility;
Keywords=forensics;images;video;triage;exif;hash;
StartupWMClass=GLEAPP
DESKTOP
  fi
  log "installed to $root (run: gleapp)"
}

do_uninstall() {
  log "removing GLEAPP"
  if command -v dpkg >/dev/null 2>&1 && dpkg -s gleapp >/dev/null 2>&1; then
    run_priv dpkg -r gleapp
  fi
  if command -v rpm >/dev/null 2>&1 && rpm -q gleapp >/dev/null 2>&1; then
    run_priv rpm -e gleapp
  fi
  run_priv rm -rf /opt/gleapp /usr/bin/gleapp /usr/share/applications/gleapp.desktop \
    /usr/share/icons/hicolor/scalable/apps/gleapp.svg \
    /usr/share/icons/hicolor/256x256/apps/gleapp.png 2>/dev/null || true
  rm -rf "$HOME/.local/share/gleapp" "$HOME/.local/bin/gleapp" \
    "$HOME/.local/share/applications/gleapp.desktop" \
    "$HOME/.local/share/icons/hicolor/scalable/apps/gleapp.svg" \
    "$HOME/.local/share/icons/hicolor/256x256/apps/gleapp.png" 2>/dev/null || true
  command -v update-desktop-database >/dev/null 2>&1 && run_priv update-desktop-database \
    /usr/share/applications >/dev/null 2>&1 || true
  log "done"
}

main() {
  need_cmd curl
  need_cmd sha256sum
  [ "$(uname -s)" = Linux ] || err "this installer covers Linux; see the README for Windows and macOS"
  [ "$(uname -m)" = x86_64 ] || err "release binaries are x86_64 only; see the README to build from source"

  if [ "$UNINSTALL" = 1 ]; then do_uninstall; exit 0; fi

  if [ "$MODE" = auto ]; then
    if have_sudo; then MODE=system; else MODE=user; fi
  fi
  if [ "$MODE" = system ] && ! have_sudo; then
    log "no root or sudo; falling back to --user"
    MODE=user
  fi

  tag="$(latest_version)"
  ver="${tag#v}"
  base="https://github.com/$REPO/releases/download/$tag"
  log "GLEAPP $tag ($MODE install)"

  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  download "$base/SHA256SUMS.txt" "$tmp/SHA256SUMS.txt"

  ids="$(os_id)"
  asset=""
  case "$ids" in
    *debian*|*ubuntu*|*mint*|*pop*)
      if [ "$MODE" = system ]; then asset="gleapp_${ver}_amd64.deb"; fi ;;
    *fedora*|*rhel*|*centos*|*suse*|*opensuse*)
      if [ "$MODE" = system ]; then asset="gleapp-${ver}-1.x86_64.rpm"; fi ;;
  esac
  if [ -z "$asset" ]; then
    case "$ids" in
      *arch*) log "tip: 'yay -S gleapp-bin' (release) or 'yay -S gleapp-git' (every commit) instead" ;;
    esac
    asset="GLEAPP-${ver}-linux-x64.tar.gz"
  fi

  download "$base/$asset" "$tmp/$asset"
  verify "$tmp" "$asset"

  case "$asset" in
    *.deb)
      if command -v apt-get >/dev/null 2>&1; then run_priv apt-get install -y "$tmp/$asset";
      else run_priv dpkg -i "$tmp/$asset"; fi ;;
    *.rpm)
      if command -v dnf >/dev/null 2>&1; then run_priv dnf install -y "$tmp/$asset";
      elif command -v zypper >/dev/null 2>&1; then run_priv zypper --non-interactive install "$tmp/$asset";
      elif command -v yum >/dev/null 2>&1; then run_priv yum install -y "$tmp/$asset";
      else run_priv rpm -Uvh "$tmp/$asset"; fi ;;
    *.tar.gz) install_tarball "$tmp/$asset" "$MODE" ;;
  esac

  if command -v gleapp >/dev/null 2>&1; then
    log "installed: $(gleapp --version 2>/dev/null || echo GLEAPP)"
  else
    log "installed; start a new shell if 'gleapp' is not found (check ~/.local/bin on PATH)"
  fi
}

main "$@"

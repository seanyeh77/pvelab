#!/bin/bash
# install.sh - install pvelab (root: /usr/local/bin, other users: ~/.local/bin, no sudo needed).
#
#   curl -fsSL https://seanyeh77.github.io/pvelab/install.sh | bash
#
# Re-run it any time to update. pvelab itself needs root: when a normal user runs it,
# it asks and then re-runs the command with sudo.
# Environment: PVELAB_URL (where to download pvelab from), PVELAB_BIN (install dir)
set -euo pipefail

URL=${PVELAB_URL:-https://seanyeh77.github.io/pvelab/pvelab}
if [ "$(id -u)" -eq 0 ]; then BIN=${PVELAB_BIN:-/usr/local/bin}; else BIN=${PVELAB_BIN:-$HOME/.local/bin}; fi
DEST=$BIN/pvelab

die()  { echo "install: $*" >&2; exit 1; }
warn() { echo "install: WARNING: $*" >&2; }

command -v curl >/dev/null    || die "curl is required"
command -v python3 >/dev/null || die "python3 is required"
{ [ -d /etc/pve ] && command -v qm >/dev/null; } || [ -x /usr/sbin/qm ] \
  || warn "this does not look like a Proxmox VE host - pvelab only works on PVE"

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
echo ">> downloading $URL"
curl -fsSL "$URL" -o "$tmp" || die "download failed"
head -1 "$tmp" | grep -q '^#!/usr/bin/env python3' \
  || die "downloaded file is not pvelab (an HTML or error page? check that $URL is the raw file)"
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp" \
  || die "downloaded file is not valid Python"

mkdir -p "$BIN"
[ -e "$DEST" ] && cmp -s "$tmp" "$DEST" && { echo ">> $DEST is already up to date"; } \
  || { install -m 755 "$tmp" "$DEST"; echo ">> installed $DEST"; }

# make sure ~/.local/bin is on PATH for new shells
case ":$PATH:" in
  *":$BIN:"*) on_path=1 ;;
  *) on_path=0
     line="export PATH=\"$BIN:\$PATH\""
     if ! grep -qsF "$line" "$HOME/.bashrc"; then
       printf '\n# added by the pvelab installer\n%s\n' "$line" >> "$HOME/.bashrc"
       echo ">> added $BIN to PATH in ~/.bashrc"
     fi ;;
esac

# pvelab needs root; normal users go through sudo
if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null; then
    warn "sudo is not installed - run pvelab as root instead"
  elif ! id -nG | tr ' ' '\n' | grep -qxE 'sudo|wheel'; then
    warn "$(id -un) is not in the sudo group - pvelab's sudo step may be refused"
  fi
fi

echo
echo "Done. Try:"
[ "$on_path" = 1 ] || echo "  source ~/.bashrc      # or open a new shell"
echo "  pvelab                # usage"
echo "  pvelab templates      # asks for sudo if you are not root"

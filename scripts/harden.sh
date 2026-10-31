#!/bin/bash
# Only "student" may ssh in; passwords need 10+ characters
# Why: botnets guess the weak passwords on accounts students make in class (adduser bob).
# Those accounts still work locally (su - bob), just not over ssh. Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# passwords: at least 10 characters, and root (sudo passwd bob) is held to it too
# (package first: if it fails, nothing is half changed; wait for apt-daily's lock after first boot)
apt-get update -q -o DPkg::Lock::Timeout=300
apt-get install -y -q -o DPkg::Lock::Timeout=300 libpam-pwquality
mkdir -p /etc/security/pwquality.conf.d
printf 'minlen = 10\nenforce_for_root\n' > /etc/security/pwquality.conf.d/pvelab.conf

# ssh: only the student account may log in
mkdir -p /etc/ssh/sshd_config.d
printf 'AllowUsers student\n' > /etc/ssh/sshd_config.d/10-pvelab.conf

# Ubuntu 24.04+ starts sshd on the first connection (socket): not running yet is fine,
# it reads the new config when it starts
systemctl try-reload-or-restart ssh 2>/dev/null || systemctl try-reload-or-restart sshd 2>/dev/null || true

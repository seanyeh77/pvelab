#!/bin/bash
# Strong passwords: 10+ characters, no dictionary words, for root too
# Why: botnets guess weak passwords over ssh, e.g. on accounts students make in class (adduser bob).
# Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# wait for apt-daily's lock after first boot
apt-get update -q -o DPkg::Lock::Timeout=300
apt-get install -y -q -o DPkg::Lock::Timeout=300 libpam-pwquality
# enforce_for_root: root (sudo passwd bob) is held to it too
mkdir -p /etc/security/pwquality.conf.d
printf 'minlen = 10\nenforce_for_root\n' > /etc/security/pwquality.conf.d/pvelab.conf

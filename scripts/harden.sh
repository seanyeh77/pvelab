#!/bin/bash
# Strong passwords (10+ characters, no dictionary words, root too) and fail2ban against ssh password guessing
# Why: botnets guess weak passwords over ssh, e.g. on accounts students make in class (adduser bob).
# Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# wait for apt-daily's lock after first boot
apt-get update -q -o DPkg::Lock::Timeout=300
apt-get install -y -q -o DPkg::Lock::Timeout=300 libpam-pwquality fail2ban python3-systemd

# Passwords, checked when one is set (passwd, adduser): 10+ characters, not a dictionary word or the
# user name (pwquality's defaults), no aaaa or 1234 runs longer than 3. No rule to mix character classes:
# like NIST SP 800-63B, length and a word list matter, forced classes give Password1!.
# enforce_for_root: root (sudo passwd bob) is held to it too.
mkdir -p /etc/security/pwquality.conf.d
printf 'minlen = 10\nmaxrepeat = 3\nmaxsequence = 3\nenforce_for_root\n' > /etc/security/pwquality.conf.d/pvelab.conf

# fail2ban: 5 failed ssh logins within 10 minutes ban that address for an hour. Read from the journal
# (Debian 13 has no auth.log). The gateway is never banned: students who come in through the host
# all arrive from it.
gw="$(ip -4 route show default | awk '{print $3; exit}')"
if command -v nft >/dev/null; then action=nftables-multiport; else action=iptables-multiport; fi
cat > /etc/fail2ban/jail.d/pvelab.local <<JAIL
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 ${gw}
banaction = ${action}
backend = systemd

[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
JAIL
systemctl enable fail2ban
systemctl restart fail2ban

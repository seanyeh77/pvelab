"""Cutting internet / ssh per VM (mangle FORWARD DROP), kept in /etc/network/interfaces."""

import json
import re
import shlex
import subprocess

from .config import BLOCK_KINDS, BLOCKS_FILE, IFACES, UPLINK
from .credentials import cred_rows
from .ifaces import block_re, bridge_is_ours
from .inventory import read_conf
from .util import run


# Rules live in the mangle table: it is walked before the filter table, so they win over the
# net's ACCEPT rules no matter in which order those were added. Written into the net's block in
# /etc/network/interfaces (post-up/post-down) so they come back after a reboot.
def blocks_load():
    try:
        return {int(k): set(v) for k, v in json.load(open(BLOCKS_FILE)).items()}
    except (OSError, ValueError):
        return {}


def blocks_save(b):
    with open(BLOCKS_FILE, "w") as f:
        json.dump({str(k): sorted(v) for k, v in sorted(b.items()) if v}, f, indent=1)


def vm_mac(vmid):
    m = re.search(r"=([0-9A-Fa-f:]{17})", (read_conf(vmid) or {}).get("net0", ""))
    return m.group(1).lower() if m else None


def block_rules(n, blocks, rows):
    """mangle FORWARD rules for the blocked VMs of net n. Each VM is matched by its MAC and by its
    IP, so a student would have to change both to slip through."""
    rules = []
    for v in sorted(blocks):
        r = rows.get(v)
        if not r or r["net"] != n or r["ip"] == "-":
            continue
        ip, mac = r["ip"], vm_mac(v)
        if "internet" in blocks[v]:              # leave replies to ssh from outside (DNAT) alone
            keep = "-m conntrack ! --ctstate DNAT -j DROP"
            rules.append(f"FORWARD -i vmbr{n} -o {UPLINK} -s {ip} {keep}")
            if mac:
                rules.append(f"FORWARD -i vmbr{n} -o {UPLINK} -m mac --mac-source {mac} {keep}")
        if "ssh" in blocks[v]:                   # also cuts sessions that are already open
            rules.append(f"FORWARD -i {UPLINK} -o vmbr{n} -d {ip} -p tcp --dport 22 -j DROP")
    return rules


def apply_blocks(n):
    """Rewrite net n's block section from BLOCKS_FILE and apply it now."""
    s = open(IFACES).read()
    old = block_re(n).search(s)
    if old:
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
        s = s.replace(old.group(0), "")
    rules = block_rules(n, blocks_load(), cred_rows()) if bridge_is_ours(n) else []
    if rules:
        section = (f"        # block net {n} begin\n"
                   + "".join(f"        post-up   iptables -t mangle -A {r}\n" for r in rules)
                   + "".join(f"        post-down iptables -t mangle -D {r}\n" for r in rules)
                   + f"        # block net {n} end\n")
        s = s.replace(f"# lab.sh net {n} end\n", section + f"# lab.sh net {n} end\n")
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    with open(IFACES, "w") as f:
        f.write(s)
    for r in rules:
        run("iptables", "-t", "mangle", "-A", *shlex.split(r))


def block_label(vmid, blocks):
    k = blocks.get(vmid, set())
    return ",".join(x for x in BLOCK_KINDS if x in k) or "-"

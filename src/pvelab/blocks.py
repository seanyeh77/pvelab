"""Cutting internet / ssh per VM (mangle FORWARD DROP), kept in /etc/network/interfaces."""

import json

from .config import BLOCK_KINDS, BLOCKS_FILE, MINING_PORTS, UPLINK
from .credentials import cred_rows
from .ifaces import bridge_is_ours, write_section
from .inventory import vm_mac


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


def net_rules(n):
    """For every VM of net n: no new connections to common mining-pool (stratum) ports."""
    return [f"FORWARD -i vmbr{n} -o {UPLINK} -p {p} -m multiport --dports {MINING_PORTS} "
            f"-m conntrack --ctstate NEW -j DROP" for p in ("tcp", "udp")]


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


def apply_blocks(n, on=True):
    """Rewrite net n's block section (net_rules + BLOCKS_FILE) and apply it now; on=False removes it."""
    rules = net_rules(n) + block_rules(n, blocks_load(), cred_rows()) if on and bridge_is_ours(n) else []
    write_section("block", n, [f"iptables -t mangle -A {r}" for r in rules],
                  [f"iptables -t mangle -D {r}" for r in rules])


def block_label(vmid, blocks):
    k = blocks.get(vmid, set())
    return ",".join(x for x in BLOCK_KINDS if x in k) or "-"

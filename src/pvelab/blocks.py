"""Cutting internet / ssh per VM (mangle FORWARD DROP), kept in /etc/network/interfaces; which VMs are
blocked: their registry records."""

import json

from . import registry
from .config import BLOCK_KINDS, BLOCKS_FILE, bridge, MINING_PORTS, UPLINK
from .credentials import cred_rows
from .ifaces import iptables, write_section
from .inventory import vm_mac


# Rules live in the mangle table: it is walked before the filter table, so they win over the
# net's ACCEPT rules no matter in which order those were added. Written into the net's block in
# /etc/network/interfaces (post-up/post-down) so they come back after a reboot.
def blocks_load():
    """{vmid: set of BLOCK_KINDS} blocked: from the VMs' records, and BLOCKS_FILE's entries of the VMs
    without one (not migrated yet)."""
    recs = registry.records()
    try:
        old = {int(k): set(v) for k, v in json.load(open(BLOCKS_FILE)).items()}
    except (OSError, ValueError):
        old = {}
    out = {v: b for v, b in old.items() if v not in recs}
    out.update({v: set(rec["blocks"]) for v, rec in recs.items() if rec.get("blocks")})
    return dict(sorted(out.items()))


def blocks_save(b):
    """blocks_load()'s {vmid: set} as the caller changed it: the VMs whose blocks changed get them in their
    records (none: the field dropped); the others stay where they are."""
    from .legacy import write                    # legacy reads VMs through inventory, which imports credentials
    now = blocks_load()
    write({v: {"blocks": sorted(b.get(v) or ()) or None} for v in sorted(set(b) | set(now))
           if set(b.get(v) or ()) != now.get(v, set())})


def net_rules(n):
    """For every VM of net n: no new connections to common mining-pool (stratum) ports."""
    return [f"FORWARD -i {bridge(n)} -o {UPLINK} -p {p} -m multiport --dports {MINING_PORTS} "
            f"-m conntrack --ctstate NEW -j DROP" for p in ("tcp", "udp")]


def block_rules(n, blocks, rows):
    """mangle FORWARD rules for the blocked VMs of net n. Each VM is matched by its MAC and by its
    IP, so a user would have to change both to slip through."""
    rules = []
    for v in sorted(blocks):
        r = rows.get(v)
        if not r or r["net"] != n or r["ip"] == "-":
            continue
        ip, mac = r["ip"], vm_mac(v)
        if "internet" in blocks[v]:              # leave replies to ssh from outside (DNAT) alone
            keep = "-m conntrack ! --ctstate DNAT -j DROP"
            rules.append(f"FORWARD -i {bridge(n)} -o {UPLINK} -s {ip} {keep}")
            if mac:
                rules.append(f"FORWARD -i {bridge(n)} -o {UPLINK} -m mac --mac-source {mac} {keep}")
        if "ssh" in blocks[v]:                   # also cuts sessions that are already open
            rules.append(f"FORWARD -i {UPLINK} -o {bridge(n)} -d {ip} -p tcp --dport 22 -j DROP")
    return rules


def apply_blocks(n, on=True):
    """Rewrite net n's block section (net_rules + BLOCKS_FILE) and apply it now; on=False removes it."""
    rules = net_rules(n) + block_rules(n, blocks_load(), cred_rows()) if on else []
    write_section("block", n, *iptables([f"-t mangle {r}" for r in rules]))


def block_label(vmid, blocks):
    k = blocks.get(vmid, set())
    return ",".join(x for x in BLOCK_KINDS if x in k) or "-"

"""Students of one net can't reach each other; one student's own VMs can.

VMs of a net share its bridge (labN or vmbrN), so they talk at layer 2 without passing iptables. An nftables
table in the bridge family (one per net) drops traffic between bridge ports that belong to different
students. Ports are the VMs' tap devices (tap<VMID>i0): a student who changes the VM's IP or MAC still
sends from the same port. Traffic routed back into the net through the host (in and out of the net's bridge) is
dropped too. The rules live in pvelab's section of /etc/network/interfaces, so they survive reboots.
"""

import os
import re
from collections import Counter

from .config import bridge, LAB_DIR
from .credentials import cred_rows
from .ifaces import has_section, iptables, write_section
from .util import write_file

NFT_DIR = f"{LAB_DIR}/nft"


def is_isolated(n):
    return has_section("isolate", n)


def nft_path(n):
    return f"{NFT_DIR}/net{n}.nft"


def nft_name(n):
    return f"pvelab_net{n}"


def student_of(name):
    """'net30-student01-2' -> 'net30-student01' (one student's VMs share it); other names as they are."""
    m = re.fullmatch(r"(.+-\D*\d+)-\d+", name)
    return m.group(1) if m else name


def by_student(vms):
    """{vmid: name} -> {student: [vmid, ...]} (VMIDs in order)."""
    out = {}
    for v, name in sorted(vms.items()):
        out.setdefault(student_of(name), []).append(v)
    return out


def vms_per_student(names):
    """How many VMs each student has on a net (from the names), or 0 for no VMs."""
    return max(Counter(map(student_of, names)).values(), default=0)


def tap(vmid):
    return f"tap{vmid}i0"


def nft_table(n, vms):
    """The nft script for net n. vms: {vmid: name} of the VMs on the net."""
    students = by_student(vms)
    pairs = [f'"{tap(a)}" . "{tap(b)}"' for ids in students.values() for a in ids for b in ids if a != b]
    elements = f"\n        elements = {{ {', '.join(pairs)} }}" if pairs else ""
    t = nft_name(n)
    return (f"table bridge {t}\ndelete table bridge {t}\n"     # replace it in one transaction
            f"table bridge {t} {{\n"
            f"    set same_student {{\n        type ifname . ifname{elements}\n    }}\n"
            f"    chain forward {{\n"
            f"        type filter hook forward priority 0; policy accept;\n"
            f'        meta ibrname != "{bridge(n)}" accept\n'
            f"        iifname . oifname @same_student accept\n"
            f"        drop\n"
            f"    }}\n}}\n")


def hairpin_rule(n):
    return f"FORWARD -i {bridge(n)} -o {bridge(n)} -j DROP"


def set_isolation(n, on=True):
    """Write (on, also after VMs came or went) or remove net n's isolate section, and apply it now."""
    if not on:
        write_section("isolate", n, [], [])
        if os.path.exists(nft_path(n)):
            os.remove(nft_path(n))
        return
    os.makedirs(NFT_DIR, exist_ok=True)
    write_file(nft_path(n), nft_table(n, {v: r["name"] for v, r in cred_rows().items() if r["net"] == n}), 0o600)
    up, down = iptables([f"-t mangle {hairpin_rule(n)}"])
    write_section("isolate", n, [f"nft -f {nft_path(n)}"] + up, [f"nft delete table bridge {nft_name(n)}"] + down)

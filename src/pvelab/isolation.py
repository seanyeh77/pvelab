"""Students of one net can't reach each other; one student's own VMs can.

VMs of a net share the bridge vmbrN, so they talk at layer 2 without passing iptables. An nftables
table in the bridge family (one per net) drops traffic between bridge ports that belong to different
students. Ports are the VMs' tap devices (tap<VMID>i0): a student who changes the VM's IP or MAC still
sends from the same port. Traffic routed back into the net through the host (-i vmbrN -o vmbrN) is
dropped too. The rules live in pvelab's section of /etc/network/interfaces, so they survive reboots.
"""

import os
import re
import shlex
import subprocess

from .config import IFACES, LAB_DIR
from .credentials import cred_rows
from .util import run

NFT_DIR = f"{LAB_DIR}/nft"


def isolate_re(n):
    return re.compile(rf"^        # isolate net {n} begin\n.*?^        # isolate net {n} end\n", re.M | re.S)


def is_isolated(n):
    return isolate_re(n).search(open(IFACES).read()) is not None


def student_of(name):
    """'net30-student01-2' -> 'net30-student01' (one student's VMs share it); other names as they are."""
    m = re.fullmatch(r"(.+-\D*\d+)-\d+", name)
    return m.group(1) if m else name


def vms_per_student(names):
    """How many VMs each student has on a net (from the names), or 0 for no VMs."""
    if not names:
        return 0
    per = {}
    for n in names:
        per[student_of(n)] = per.get(student_of(n), 0) + 1
    return max(per.values())


def tap(vmid):
    return f"tap{vmid}i0"


def nft_table(n, vms):
    """The nft script for net n. vms: {vmid: name} of the VMs on vmbrN."""
    students = {}
    for v, name in sorted(vms.items()):
        students.setdefault(student_of(name), []).append(v)
    pairs = [f'"{tap(a)}" . "{tap(b)}"' for ids in students.values() for a in ids for b in ids if a != b]
    elements = f"\n        elements = {{ {', '.join(pairs)} }}" if pairs else ""
    t = f"pvelab_net{n}"
    return (f"table bridge {t}\ndelete table bridge {t}\n"     # replace it in one transaction
            f"table bridge {t} {{\n"
            f"    set same_student {{\n        type ifname . ifname{elements}\n    }}\n"
            f"    chain forward {{\n"
            f"        type filter hook forward priority 0; policy accept;\n"
            f'        meta ibrname != "vmbr{n}" accept\n'
            f"        iifname . oifname @same_student accept\n"
            f"        drop\n"
            f"    }}\n}}\n")


def hairpin_rule(n):
    return f"FORWARD -i vmbr{n} -o vmbr{n} -j DROP"


def set_isolation(n, on):
    """Write (on) or remove net n's isolate section in its interfaces block, and apply it now."""
    s = open(IFACES).read()
    old = isolate_re(n).search(s)
    if old:
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
        s = s.replace(old.group(0), "")
    path = f"{NFT_DIR}/net{n}.nft"
    if on:
        section = (f"        # isolate net {n} begin\n"
                   f"        post-up   nft -f {path}\n"
                   f"        post-up   iptables -t mangle -A {hairpin_rule(n)}\n"
                   f"        post-down nft delete table bridge pvelab_net{n}\n"
                   f"        post-down iptables -t mangle -D {hairpin_rule(n)}\n"
                   f"        # isolate net {n} end\n")
        s = s.replace(f"# lab.sh net {n} end\n", section + f"# lab.sh net {n} end\n")
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    with open(IFACES, "w") as f:
        f.write(s)
    if on:
        update_isolation(n)
        run("iptables", "-t", "mangle", "-A", *hairpin_rule(n).split())
    elif os.path.exists(path):
        os.remove(path)


def update_isolation(n):
    """Rewrite and load net n's nft table from the current VM list (after create / destroy)."""
    if not is_isolated(n):
        return
    os.makedirs(NFT_DIR, exist_ok=True)
    vms = {v: r["name"] for v, r in cred_rows().items() if r["net"] == n}
    path = f"{NFT_DIR}/net{n}.nft"
    with open(path, "w") as f:
        f.write(nft_table(n, vms))
    run("nft", "-f", path)

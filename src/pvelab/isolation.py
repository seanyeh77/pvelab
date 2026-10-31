"""Students of one net can't reach each other; one student's own VMs can.

VMs of a net share its bridge (labN or vmbrN), so they talk at layer 2 without passing iptables. An nftables
table in the bridge family (one per net) drops traffic between bridge ports that belong to different
students. Ports are the VMs' tap devices (tap<VMID>i0): a student who changes the VM's IP or MAC still
sends from the same port. Traffic routed back into the net through the host (in and out of the net's bridge) is
dropped too. The rules live in pvelab's section of /etc/network/interfaces, so they survive reboots.

An SDN net (labN) spans the cluster's hosts. Every other host gets the same table, plus rules that let its
VMs reach only the gateway (on this host, behind the VXLAN port) and only the gateway reach them. There it
is a file in NODE_NFT_DIR, loaded at boot by pvelab-nft.service.
"""

import os
import re
from collections import Counter

from .config import bridge, is_sdn, LAB_DIR, subnet
from .credentials import cred_rows
from .ifaces import has_section, iptables, write_section
from .nodes import is_local, members, multi, run_on
from .util import warn, write_file

NFT_DIR = f"{LAB_DIR}/nft"
NODE_NFT_DIR = "/etc/pvelab/nft"             # on the other hosts: the tables of SDN nets, loaded at boot
NFT_UNIT = "pvelab-nft.service"


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


def vxlan_port(n):
    """The bridge port SDN's VXLAN zone gives vnet labN on every host (VxlanPlugin.pm: vxlan_<vnet>)."""
    return f"vxlan_{bridge(n)}"


def gw_mac(n):
    """The fixed MAC of the gateway (.1) on SDN net n's bridge labN: the other hosts' tables let VMs
    reach only this address over VXLAN (a bridge's own MAC changes as ports come and go)."""
    return f"02:00:0a:0a:{int(n):02x}:01"


def gateway_rules(n):
    """On a host without net n's gateway: its VMs may send to the gateway's MAC (and ask ARP for .1) over
    VXLAN, and receive from it; nothing else crosses to another host."""
    vx, gw = vxlan_port(n), gw_mac(n)
    return [f'oifname "{vx}" ether daddr {gw} accept',
            f'oifname "{vx}" arp operation request arp daddr ip {subnet(n)}.1 accept',
            f'iifname "{vx}" ether saddr {gw} accept']


def nft_table(n, vms, gateway=True):
    """The nft script for net n. vms: {vmid: name} of the VMs on the net. gateway: for the host with
    the net's gateway (this one); False: for another host of an SDN net."""
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
            + "".join(f"        {r}\n" for r in ([] if gateway else gateway_rules(n)))
            + "        drop\n    }\n}\n")


def hairpin_rule(n):
    return f"FORWARD -i {bridge(n)} -o {bridge(n)} -j DROP"


def net_vms(n):
    """{vmid: name} of the VMs pvelab has on net n."""
    return {v: r["name"] for v, r in cred_rows().items() if r["net"] == n}


def node_nft_path(n):
    return f"{NODE_NFT_DIR}/net{n}.nft"


def other_nodes():
    """The other online hosts of the cluster (none without one)."""
    return [node for node, m in sorted(members().items()) if m["online"] and not is_local(node)]


def offline_nodes():
    """The cluster's hosts that are offline now."""
    return [node for node, m in sorted(members().items()) if not m["online"]]


def push_nft(node, n, text):
    """Write net n's table on another host and load it now."""
    path = node_nft_path(n)
    run_on(node, "sh", "-c", f"mkdir -p {NODE_NFT_DIR} && cat > {path} && nft -f {path}", data=text)


def drop_nft(node, n):
    """Remove net n's table from another host (nothing there is fine)."""
    run_on(node, "sh", "-c", f"rm -f {node_nft_path(n)}; nft delete table bridge {nft_name(n)} 2>/dev/null; true",
           check=False, quiet=True)


def set_isolation(n, on=True):
    """Write (on, also after VMs came or went) or remove net n's isolate section, and apply it now.
    An SDN net with several hosts: the other online hosts get (or lose) the table too."""
    remote = multi() and is_sdn(n)
    if not on:
        write_section("isolate", n, [], [])
        if os.path.exists(nft_path(n)):
            os.remove(nft_path(n))
        for node in other_nodes() if remote else []:
            drop_nft(node, n)
        for node in offline_nodes() if remote else []:     # its boot unit would load the table again
            warn(f"host {node} is offline: remove net{n}'s isolation table there once it is back, as root: "
                 f"rm -f {node_nft_path(n)}; nft delete table bridge {nft_name(n)}")
        return
    os.makedirs(NFT_DIR, exist_ok=True)
    vms = net_vms(n)
    write_file(nft_path(n), nft_table(n, vms), 0o600)
    up, down = iptables([f"-t mangle {hairpin_rule(n)}"])
    write_section("isolate", n, [f"nft -f {nft_path(n)}"] + up, [f"nft delete table bridge {nft_name(n)}"] + down)
    for node in other_nodes() if remote else []:
        push_nft(node, n, nft_table(n, vms, gateway=False))


NFT_UNIT_TEXT = f"""[Unit]
Description=pvelab: isolation tables of pvelab's SDN nets ({NODE_NFT_DIR})
After=network.target
Before=pve-guests.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in {NODE_NFT_DIR}/*.nft; do [ -e "$$f" ] && nft -f "$$f"; done; exit 0'

[Install]
WantedBy=multi-user.target
"""


def ensure_nft_unit(node):
    """On another host: a boot-time unit that loads the tables in NODE_NFT_DIR (they match bridge names,
    so the bridges may come up later)."""
    path = f"/etc/systemd/system/{NFT_UNIT}"
    run_on(node, "sh", "-c", f"mkdir -p {NODE_NFT_DIR} && cat > {path} && systemctl daemon-reload && "
           f"systemctl enable {NFT_UNIT}", data=NFT_UNIT_TEXT)

"""Owners of one net can't reach each other; one owner's own VMs can.

VMs of a net share its bridge (labN or vmbrN), so they talk at layer 2 without passing iptables. An nftables
table in the bridge family (one per net) drops traffic between bridge ports that belong to different
owners, except to and from the net's shared VM (pvelab create --shared), which every owner reaches.
Ports are the VMs' tap devices (tap<VMID>i0): a user who changes the VM's IP or MAC still
sends from the same port. Traffic routed back into the net through the host (in and out of the net's bridge) is
dropped too. The rules live in pvelab's section of /etc/network/interfaces, so they survive reboots.

An SDN net (labN) spans the cluster's hosts. Every other host gets the same table, plus rules that let its
VMs reach only the gateway (on this host, behind the VXLAN port) and only the gateway reach them. There it
is a file in NODE_NFT_DIR, loaded at boot by pvelab-nft.service. A shared VM has a fixed MAC (shared_mac),
so owners on another host reach it over VXLAN the same way.
"""

import os

from . import nets
from .config import bridge, is_sdn, LAB_DIR
from .credentials import cred_rows
from .ifaces import has_section, iptables, write_section
from .nodes import is_local, multi, offline_nodes, online_nodes, run_on, write_on
from .owners import by_owner, confs_of, is_shared_vm
from .util import parallel, warn, write_file

NFT_DIR = f"{LAB_DIR}/nft"
NODE_NFT_DIR = "/etc/pvelab/nft"             # on the other hosts: the tables of SDN nets, loaded at boot
NFT_UNIT = "pvelab-nft.service"


def is_isolated(n):
    return has_section("isolate", n)


def nft_path(n):
    return f"{NFT_DIR}/net{n}.nft"


def nft_name(n):
    return f"pvelab_net{n}"


def shared_mac(n):
    """The fixed MAC of net n's shared VM: the other hosts of an SDN net let their VMs reach it by this MAC."""
    return f"02:00:0a:0a:{int(n):02x}:02"


def shared_ip(n, rows=None, confs=None):
    """The IP of net n's shared VM (from the credentials), or None. confs: VM configs read already."""
    rows = cred_rows() if rows is None else rows
    mine = {i: r for i, r in rows.items() if r["net"] == n and (confs is None or i in confs)}
    confs = confs_of(mine, confs)
    return next((r["ip"] for i, r in mine.items() if is_shared_vm(confs[i], n)), None)


def tap(vmid):
    return f"tap{vmid}i0"


def vxlan_port(n):
    """The bridge port SDN's VXLAN zone gives vnet labN on every host (VxlanPlugin.pm: vxlan_<vnet>)."""
    return f"vxlan_{bridge(n)}"


def gw_mac(n):
    """The fixed MAC of the gateway (the net's first address) on SDN net n's bridge labN: the other hosts'
    tables let VMs reach only this address over VXLAN (a bridge's own MAC changes as ports come and go)."""
    return f"02:00:0a:0a:{int(n):02x}:01"


def gateway_rules(n):
    """On a host without net n's gateway: its VMs may send to the gateway's MAC (and ask ARP for it) over
    VXLAN, and receive from it; nothing else crosses to another host."""
    vx, gw = vxlan_port(n), gw_mac(n)
    return [f'oifname "{vx}" ether daddr {gw} accept',
            f'oifname "{vx}" arp operation request arp daddr ip {nets.gateway(n)} accept',
            f'iifname "{vx}" ether saddr {gw} accept']


def shared_rules(n, vms, ip, confs=None):
    """Net n's shared VM (in vms, at ip) reaches and is reached by every port: its own tap here, and on an
    SDN net its fixed MAC (and ARP for its IP) over VXLAN, for VMs on another host."""
    confs = confs_of(vms, confs)
    taps = [tap(v) for v in sorted(vms) if is_shared_vm(confs[v], n)]
    if not taps:
        return []
    rules = [f'{d}ifname "{t}" accept' for t in taps for d in ("i", "o")]
    if is_sdn(n):
        vx, mac = vxlan_port(n), shared_mac(n)
        rules += [f'oifname "{vx}" ether daddr {mac} accept']
        rules += [f'oifname "{vx}" arp operation request arp daddr ip {ip} accept'] if ip and ip != "-" else []
        rules += [f'iifname "{vx}" ether saddr {mac} accept']
    return rules


def nft_table(n, vms, gateway=True, ip=None, confs=None):
    """The nft script for net n. vms: {vmid: row (or name)} of the VMs on the net. gateway: for the host with
    the net's gateway (this one); False: for another host of an SDN net. ip: the shared VM's (shared_ip).
    confs: the VMs' configs, if the caller has read them."""
    confs = confs_of(vms, confs)
    groups = by_owner(vms, confs)
    pairs = [f'"{tap(a)}" . "{tap(b)}"' for ids in groups.values() for a in ids for b in ids if a != b]
    elements = f"\n        elements = {{ {', '.join(pairs)} }}" if pairs else ""
    t = nft_name(n)
    return (f"table bridge {t}\ndelete table bridge {t}\n"     # replace it in one transaction
            f"table bridge {t} {{\n"
            f"    set same_student {{\n        type ifname . ifname{elements}\n    }}\n"
            f"    chain forward {{\n"
            f"        type filter hook forward priority 0; policy accept;\n"
            f'        meta ibrname != "{bridge(n)}" accept\n'
            f"        iifname . oifname @same_student accept\n"
            + "".join(f"        {r}\n" for r in shared_rules(n, vms, ip, confs) + ([] if gateway else gateway_rules(n)))
            + "        drop\n    }\n}\n")


def hairpin_rule(n):
    return f"FORWARD -i {bridge(n)} -o {bridge(n)} -j DROP"


def net_vms(n):
    """{vmid: credentials row} of the VMs pvelab has on net n."""
    return {v: r for v, r in cred_rows().items() if r["net"] == n}


def node_nft_path(n):
    return f"{NODE_NFT_DIR}/net{n}.nft"


def other_nodes():
    """The other online hosts of the cluster (none without one)."""
    return [n for n in online_nodes() if not is_local(n)]


def push_nft(node, n, text):
    """Write net n's table on another host and load it now."""
    write_on(node, node_nft_path(n), text)
    run_on(node, "nft", "-f", node_nft_path(n))


def drop_nft(node, n):
    """Remove net n's table from another host (nothing there is fine)."""
    run_on(node, "sh", "-c", f"rm -f {node_nft_path(n)}; nft delete table bridge {nft_name(n)} 2>/dev/null; true",
           check=False, quiet=True)


def host_table(n):
    """What another host needs for net n: its table if the net is isolated, else None (drop it)."""
    if not is_isolated(n):
        return None
    vms = net_vms(n)
    confs = confs_of(vms)
    return nft_table(n, vms, gateway=False, ip=shared_ip(n, confs=confs), confs=confs)


def sync_host(node, n, table):
    """Bring another host in line with net n: table (from host_table) pushed and loaded, or None: net n's
    table dropped there."""
    if table:
        push_nft(node, n, table)
    else:
        drop_nft(node, n)


def set_isolation(n, on=True):
    """Write (on, also after VMs came or went) or remove net n's isolate section, and apply it now.
    An SDN net with several hosts: the other online hosts get (or lose) the table too, all at once."""
    remote = multi() and is_sdn(n)
    table = None                             # what the other hosts get: nothing when it is off
    if on:
        os.makedirs(NFT_DIR, exist_ok=True)
        vms = net_vms(n)
        confs = confs_of(vms)                 # once: each VM's config is read
        ip = shared_ip(n, confs=confs)
        write_file(nft_path(n), nft_table(n, vms, ip=ip, confs=confs), 0o600)
        up, down = iptables([f"-t mangle {hairpin_rule(n)}"])
        write_section("isolate", n, [f"nft -f {nft_path(n)}"] + up, [f"nft delete table bridge {nft_name(n)}"] + down)
        table = nft_table(n, vms, gateway=False, ip=ip, confs=confs)
    else:
        write_section("isolate", n, [], [])
        if os.path.exists(nft_path(n)):
            os.remove(nft_path(n))
    if not remote:
        return
    parallel(lambda node: sync_host(node, n, table), other_nodes())
    for node in [] if on else offline_nodes():      # its boot unit would load the table again
        warn(f"host {node} is offline: remove net{n}'s isolation table there once it is back, as root: "
             f"rm -f {node_nft_path(n)}; nft delete table bridge {nft_name(n)}")


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
    write_on(node, f"/etc/systemd/system/{NFT_UNIT}", NFT_UNIT_TEXT)
    run_on(node, "sh", "-c", f"mkdir -p {NODE_NFT_DIR} && systemctl daemon-reload && systemctl enable --quiet {NFT_UNIT}")

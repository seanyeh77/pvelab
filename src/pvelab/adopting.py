"""VMs made by hand (PVE's web UI) on one of pvelab's nets: what they have (their network cards, cloud-init
address and user, the addresses their guest agent reports), finding them (strays: pvelab list names them),
and the checks pvelab adopt makes before it records one as pvelab's.

pvelab's nets here are the numbered nets of nets.all_nets() (its own blocks, SDN labN, PVE vnets it took
over); not the DHCP bridge (vmbr1) nor an admin's bridges (vmbr0): only the numbered nets have pvelab's
rules, addresses and owners."""

import re
from collections import namedtuple

from . import accounts, config, nets
from .inventory import is_managed

Stray = namedtuple("Stray", "vmid name net nic")
NIC = re.compile(r"net(\d+)")
MAC = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")


def nics(conf):
    """[(key, bridge, mac)] of a VM config's network cards net0, net1, ... in number order (bridge= and the
    MAC of MODEL=MAC, lower case; None when the value has none)."""
    out = []
    for key in sorted((k for k in conf if NIC.fullmatch(k)), key=lambda k: int(k[3:])):
        opts = dict(p.partition("=")[::2] for p in str(conf[key]).split(",") if p)
        mac = next((v for k, v in opts.items() if k != "bridge" and MAC.fullmatch(v)), None)
        out.append((key, opts.get("bridge") or None, mac.lower() if mac else None))
    return out


def pvelab_bridges(found=None):
    """{bridge: net number} of pvelab's nets (found: nets.all_nets(), if read)."""
    return {config.bridge(n): n for n in (nets.all_nets() if found is None else found)}


def strays(inv, found=None):
    """The VMs of inv (inventory()) that are not pvelab's and have a card on one of its nets - the first such
    card, net0 first - in VMID order. Templates are left out."""
    bridges, out = pvelab_bridges(found), []
    for vmid, c in sorted(inv.items()):
        if not c or is_managed(c) or c.get("template") == "1":
            continue
        for key, br, _ in nics(c):
            if br in bridges:
                out.append(Stray(vmid, c.get("name") or str(vmid), bridges[br], key))
                break
    return out


def adopt_arg(s, inv):
    """How pvelab adopt names stray s: its name when no other VM of inv has it and VM arguments read it as a
    name, else its VMID."""
    name = (inv.get(s.vmid) or {}).get("name") or ""
    if name and accounts.vm_name_problem(name) is None and \
            sum(1 for c in inv.values() if (c or {}).get("name") == name) == 1:
        return name
    return str(s.vmid)


def stray_line(s, inv, shared):
    """pvelab list's line for stray s: pvelab leaves it alone, and the command that hands it over. shared: the
    net numbers that have their shared VM already."""
    head = f"{s.name} ({s.vmid}) on {nets.net_name(s.net)}"
    if s.nic != "net0":
        return (f"{head} ({s.nic}): not made by pvelab, so pvelab leaves it alone; pvelab adopt takes a VM whose "
                "net0 is on its net")
    whose = "--owner NAME" if s.net in shared else "--shared (or --owner NAME)"
    return f"{head}: not made by pvelab, so pvelab leaves it alone; pvelab adopt {adopt_arg(s, inv)} {whose} hands it over"

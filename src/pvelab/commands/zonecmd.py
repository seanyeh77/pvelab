"""pvelab zone [list] | zone set ZONE --nodes A,B|all [--peers auto] [--dry-run] | zone remove ZONE [--dry-run]
PVE's SDN zones: listed with their hosts, peers and nets; a VXLAN zone's hosts and peers changed; an empty
zone removed. Everything is read from PVE's SDN config: pvelab records nothing about zones."""

import os
import re

from ..args import opt
from .. import config, ifaces, nets, nodes, sdn, ui, zones
from ..helptext import bad_usage
from ..networks import guests_on
from ..nodes import run_on
from ..ui import paint, Plan
from ..util import die

USAGE = "zone takes: list, set ZONE --nodes A,B|all [--peers auto] [--dry-run], set ZONE --peers auto, or remove ZONE"


def zone_vnets(z):
    """{vnet: its alias or None} of every vnet in zone z (pvelab's or not)."""
    return {v: alias for v, (zone, alias) in nets.sdn_vnets().items() if zone == z}


def net_label(vnet, alias):
    """A vnet as zone list names it: its alias (pvelab's "pvelab NAME" without the prefix), else its id."""
    return re.sub(r"^pvelab ", "", alias) if alias else vnet


def cmd_zone(args):
    """zone, zone list: every SDN zone with its hosts, peers and nets."""
    if args and args[0] != "list" or len(args) > 1:
        bad_usage(USAGE)
    zs = zones.zones()
    if not zs:
        print("No SDN zones yet. pvelab net create NAME --zone ZONE makes one.")
        return
    addrs = zones.addresses()
    fmt = "{:<9} {:<7} {:<20} {:<8} {}"
    print(fmt.format("ZONE", "TYPE", "HOSTS", "PEERS", "NETS"))
    drift = []
    for z, found in zs.items():
        if found["type"] != "vxlan":
            peers = "-"
        elif zones.hand_tuned(z, addrs, zs):
            peers = "by hand"
            drift.append(zones.drift_line(z, addrs, zs))
        else:
            peers = "auto"
        names = [net_label(v, a) for v, a in sorted(zone_vnets(z).items())]
        print(fmt.format(z, found["type"], zones.hosts_text(found["nodes"]), peers, ", ".join(names) or "-"))
    if drift:
        print()
        for line in drift:
            print(paint(line, "yellow"))


def vxlan_zone(z):
    """Zone z's zones.cfg entry; die unless it is a VXLAN zone."""
    found = zones.zones().get(zones.valid_id(z))
    if not found:
        die(f"no SDN zone {z} (pvelab zone list shows them)")
    if found["type"] != "vxlan":
        die(f"{z} is a {found['type']} zone: pvelab changes VXLAN zones only. Change it in PVE "
            "(Datacenter → SDN)")
    return found


def guest_name(f):
    """A guest's name from its config file (a VM's name, a container's hostname), else its VMID."""
    m = re.search(r"^(?:name|hostname): (\S+)", open(f).read(), re.M)
    return m.group(1) if m else os.path.basename(f)[:-5]


def refuse_dropped(z, dropped):
    """Die when a host leaving zone z still has guests on any of its vnets, has the gateway of one of its
    pvelab nets, or is offline (its gateways can't be checked) while the zone has pvelab nets."""
    vnets = zone_vnets(z)
    for h in sorted(dropped):
        names = sorted(guest_name(f) for v in vnets for _, f in guests_on(v) if f.split("/")[-3] == h)
        if names:
            die(f"{h} has guests on {z}'s vnets: {ui.some(names, 8)}. Move them to one of the hosts kept "
                f"(pvelab move VM... --to HOST), or keep {h} in --nodes")
    own = {re.fullmatch(r"lab(\d+)", v).group(1): net_label(v, a) for v, a in vnets.items()
           if nets.pvelabs_own(v, z)}
    if not own:
        return
    for h in sorted(dropped):
        if not nodes.is_online(h):
            die(f"can't check {h} for gateways of {z}'s nets: it is offline. Start it first, or keep it in "
                "--nodes")
        there = ifaces.blocks(run_on(h, "cat", config.IFACES, quiet=True))
        gws = [name for n, name in sorted(own.items()) if n in there]
        if gws:
            die(f"{h} has the gateway of {', '.join(gws)} ({z}'s nets): it stays one of {z}'s hosts. Keep {h} "
                "in --nodes, or remove those nets first (pvelab net remove NAME)")


def cmd_zone_set(args, dry=False):
    """zone set ZONE --nodes A,B|all [--peers auto]: a VXLAN zone's hosts (automatic peers follow them;
    peers tuned by hand are left), or --peers auto: its peers aligned with its hosts' addresses."""
    z, on, peers_opt = None, None, None
    it = iter(args[1:])
    for a in it:
        if (v := opt(a, it, "--nodes")) is not None:
            on = zones.parse_nodes(v, every=True)
        elif (v := opt(a, it, "--peers")) is not None:
            if v != "auto":
                bad_usage("--peers takes auto: the zone's hosts' addresses (other peers are set in PVE)")
            peers_opt = v
        elif a.startswith("-") or z:
            bad_usage(USAGE)
        else:
            z = a
    if not z or on is None and peers_opt is None:
        bad_usage(USAGE)
    found = vxlan_zone(z)
    zs, addrs, everyone = zones.zones(), zones.addresses(), sorted(nodes.members())
    plan = Plan()
    new_on = found["nodes"]
    if on is not None:
        zones.check_hosts(on)
        new_on = on or None
        if set(new_on or everyone) == set(found["nodes"] or everyone):
            die(f"{z} is on {zones.hosts_text(found['nodes'])} already")
        refuse_dropped(z, set(found["nodes"] or everyone) - set(new_on or everyone))
        plan.do(f"SDN zone {z}: hosts {zones.hosts_text(found['nodes'])} -> {zones.hosts_text(new_on)}")
    after = dict(zs, **{z: dict(found, nodes=new_on)})
    peers = None
    if peers_opt or not zones.hand_tuned(z, addrs, zs):
        auto = zones.auto_peers(z, addrs, after)
        if auto and set(auto) != set(found["peers"] or []):
            peers = auto
            plan.do(f"SDN zone {z}: VXLAN peers {', '.join(found['peers'] or []) or '-'} -> {', '.join(auto)}")
    else:
        plan.warn(zones.drift_line(z, addrs, zs))
    if on is None and peers is None:
        print(f"{z}'s peers are its hosts' addresses already ({', '.join(found['peers'] or [])})")
        return
    plan.warn(sdn.RELOAD_WARNING)
    if not plan.go(dry):
        return
    with sdn.session():
        sdn.set_zone(z, nodes=None if on is None else on, peers=peers)
        sdn.apply()
    print(paint(f"{z} is on {zones.hosts_text(new_on)}" + (f", VXLAN peers {', '.join(peers)}" if peers else ""),
                "green"))


def cmd_zone_remove(args, dry=False):
    """zone remove ZONE: an SDN zone without any vnet (pvelab's or not)."""
    if len(args) != 2 or args[1].startswith("-"):
        bad_usage(USAGE)
    z = zones.valid_id(args[1])
    found = zones.zones().get(z)
    if not found:
        die(f"no SDN zone {z} (pvelab zone list shows them)")
    vnets = sorted(zone_vnets(z))
    if vnets:
        die(f"{z} still has vnets: {ui.some(vnets, 8)}. Remove them first (pvelab net remove NAME, or in PVE)")
    plan = Plan()
    plan.do(f"delete SDN zone {z} ({found['type']}, on {zones.hosts_text(found['nodes'])})")
    plan.warn(sdn.RELOAD_WARNING)
    if not plan.go(dry):
        return
    with sdn.session():
        sdn.delete_zone(z)
        sdn.apply()
    print(paint(f"{z} removed", "green"))

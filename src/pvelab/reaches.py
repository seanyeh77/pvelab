"""pvelab reach: a VM that reaches the VMs of every other net of its SDN zone (a VPN server such as WireGuard,
a monitoring VM). Which VMs: their records (reach: true); the rules: their net's reach section in
/etc/network/interfaces, made again whenever the net's VMs change (networks.refresh_net) and whenever a net
of this host comes or goes (refresh_all), so they follow the VM's address and the zone's nets.

Only the VM's own connections cross (and what answers them): the other nets still do not reach it, nor
each other. Only nets whose gateway is on this host, as the VM's own: what crosses nets is routed here."""

from . import registry
from .config import bridge, in_sdn
from .ifaces import has_section, iptables, our_nets, write_section
from .nets import sdn_vnets

KIND = "reach"


def zone_of(n, vnets=None):
    """Net n's SDN zone, or None for a net that is no vnet in PVE's SDN config. vnets: sdn_vnets(), if read."""
    if not in_sdn(n):
        return None
    return (sdn_vnets() if vnets is None else vnets).get(bridge(n), (None, None))[0]


def zone_nets(z, but):
    """The nets of zone z whose gateway (and rules) is on this host, but net but."""
    vnets = sdn_vnets()
    return [m for m in our_nets() if m != str(but) and zone_of(m, vnets) == z]


def rules(n, ip, targets):
    """FORWARD rules of a VM at address ip on net n that reaches the nets targets: its connections out to each
    and what answers them (each net's own rules drop what crosses nets)."""
    if not targets:
        return []
    br = bridge(n)
    return [f"FORWARD -i {br} -s {ip} -o {bridge(m)} -j ACCEPT" for m in targets] \
        + [f"FORWARD -o {br} -d {ip} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]


def on_net(n):
    """Whether a record of a VM on net n has reach (the registry file alone: no VM config read)."""
    return any(r.get(KIND) and str(r.get("net")) == str(n) for r in registry.load()["vms"].values())


def set_reach(n, on=True, recs=None):
    """(Re)write net n's reach section from its VMs' records (recs: these, as they are to be) and apply it
    now; on=False removes it. Called by pvelab reach, through refresh_net when VMs come and go, and by
    refresh_all when a net of this host comes or goes."""
    if not on:
        return write_section(KIND, n, [], [])
    mine = {v: r for v, r in (registry.records(has=KIND) if recs is None else recs).items()
            if r.get(KIND) and str(r.get("net")) == str(n) and r.get("ip")}
    z = zone_of(n) if mine else None
    targets = zone_nets(z, n) if z else []
    up, down = iptables([rule for v in sorted(mine) for rule in rules(n, mine[v]["ip"], targets)], add="-I")
    write_section(KIND, n, up, down)


def refresh_all():
    """Every reach section of this host made again: a net of this host came or went, so a zone's nets changed."""
    for m in our_nets():
        if has_section(KIND, m) or on_net(m):
            set_reach(m)

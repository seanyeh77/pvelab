"""PVE's SDN zones, read from the cluster-wide zones.cfg: pvelab records nothing about them.

A zone's hosts are its `nodes`, or every host when it has none. A VXLAN zone's automatic peers are its
hosts' addresses; peers that differ from those (as a set) were tuned by hand and are left as they are."""

import re
import socket

from . import config, nodes
from .util import die, warn

ID = re.compile(r"[a-zA-Z][a-zA-Z0-9]*[a-zA-Z0-9]")


def valid_id(name):
    """name, if PVE takes it as a zone id; else die."""
    if not (2 <= len(name) <= 8 and ID.fullmatch(name)):
        die(f"zone name {name}: 2 to 8 letters or digits, starting with a letter (PVE's rule)")
    return name


def _list(body, key):
    m = re.search(rf"^\s*{key} (.*?)\s*$", body, re.M)
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else None


def zones():
    """{zone: {"type", "nodes" (list or None), "peers" (list or None), "mtu" (int or None)}} from zones.cfg."""
    out = {}
    for m in re.finditer(r"^(\w+): (\S+)\n((?:[ \t]+.*\n?)*)", config.read_text(config.SDN_ZONES), re.M):
        kind, z, body = m.groups()
        mtu = re.search(r"^\s*mtu (\d+)", body, re.M)
        out[z] = {"type": kind, "nodes": _list(body, "nodes"), "peers": _list(body, "peers"),
                  "mtu": int(mtu.group(1)) if mtu else None}
    return out


def _ring_addrs():
    """{host: ring0_addr} of corosync.conf's nodes whose ring0_addr is an IPv4 address."""
    out = {}
    for block in nodes.corosync_blocks(config.read_text(nodes.COROSYNC_CONF)):
        name, ring = re.search(r"\bname:\s*(\S+)", block), re.search(r"ring0_addr:\s*(\S+)", block)
        if name and ring and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ring.group(1)):
            out[name.group(1)] = ring.group(1)
    return out


def addresses():
    """{host: IPv4} of every cluster host (from .members with several hosts; a host that is off has no ip
    there: its ring0 address in corosync.conf); else this host's own: corosync's ring0 address, or the uplink's."""
    if nodes.multi():
        ring = _ring_addrs()
        found = {n: m["ip"] or ring.get(n) for n, m in nodes.members().items()}
        return {n: ip for n, ip in found.items() if ip}
    names = re.findall(r"ring0_addr:\s*(\S+)", config.read_text(nodes.COROSYNC_CONF))
    ip = socket.gethostbyname(names[0]) if names else nodes.uplink_ip()
    return {nodes.this_node(): ip or die(f"no IPv4 address on {config.UPLINK} for SDN's VXLAN")}


def hosts(z, zs=None):
    """The hosts of zone z (its nodes), or None: every host."""
    return ((zones() if zs is None else zs).get(z) or {}).get("nodes")


def auto_peers(z, addrs, zs=None):
    """The VXLAN peers zone z should have: the sorted addresses of its hosts found in addrs."""
    on = hosts(z, zs)
    return sorted(ip for h, ip in addrs.items() if on is None or h in on)


def hand_tuned(z, addrs=None, zs=None):
    """Whether VXLAN zone z's peers differ (as a set) from its hosts' addresses."""
    zs = zones() if zs is None else zs
    found = zs.get(z)
    if not found or found["type"] != "vxlan":
        return False
    addrs = addresses() if addrs is None else addrs
    return set(found["peers"] or []) != set(auto_peers(z, addrs, zs))


def drift_line(z, addrs=None, zs=None):
    """One line on zone z's hand-tuned peers: what they are, what they would be, how to align them."""
    zs = zones() if zs is None else zs
    addrs = addresses() if addrs is None else addrs
    cur = ", ".join((zs.get(z) or {}).get("peers") or [])
    auto = ", ".join(auto_peers(z, addrs, zs))
    return (f"{z}: peers {cur} differ from its hosts' addresses ({auto}): left as they are "
            f"(pvelab zone set {z} --peers auto aligns them)")


def peer_plan(before, after, zs=None):
    """({zone: new peers}, [drift lines]) when the hosts' addresses go from before to after ({host: IPv4}):
    a VXLAN zone whose peers are its hosts' addresses before follows them; one already matching after
    needs nothing; any other was tuned by hand and is left, with a drift line."""
    zs = zones() if zs is None else zs
    changes, drift = {}, []
    for z, found in zs.items():
        if found["type"] != "vxlan":
            continue
        cur, new = set(found["peers"] or []), auto_peers(z, after, zs)
        if cur == set(new):
            continue
        if cur == set(auto_peers(z, before, zs)) and new:
            changes[z] = new
        else:
            drift.append(drift_line(z, after, zs))
    return changes, drift


_chosen = {}               # {net: (zone, its hosts or None)} for nets this run makes


def choose(n, zone, hosts):
    """This run's zone for new net n, and the hosts of that zone if it is made (None: every host)."""
    _chosen[str(n)] = (zone, hosts)


def chosen_nodes(n):
    """The hosts chosen for new net n's zone, or None."""
    return _chosen.get(str(n), (None, None))[1]


def net_zone(n):
    """Net n's zone: the one chosen this run, else the one vnets.cfg has for its vnet, else the default."""
    from . import nets, sdn                       # sdn imports this module
    if str(n) in _chosen:
        return _chosen[str(n)][0]
    return nets.sdn_vnets().get(sdn.vnet(n), (None, None))[0] or config.SDN_ZONE


def net_hosts(n):
    """The hosts net n's VMs may go to: [here] for a plain bridge (or "dhcp"), else its zone's hosts
    (None: every host; a zone this run makes: the hosts chosen for it)."""
    from .placement import bridge_host           # placement reads resources: imported when needed
    pin = bridge_host(n)
    if pin:
        return [pin]
    z, zs = net_zone(n), zones()
    return hosts(z, zs) if z in zs else chosen_nodes(n)


def hosts_text(on):
    """A zone's hosts as words: 'a, b', or 'every host' (None)."""
    return ", ".join(on) if on else "every host"


def parse_nodes(v, every=False):
    """--nodes a,b -> ["a", "b"] (every: "all" -> [], every host), or die."""
    if every and v == "all":
        return []
    found = [h.strip() for h in v.split(",") if h.strip()]
    if not found:
        die(f"--nodes {v}: host names separated by commas" + (", or all" if every else ""))
    return list(dict.fromkeys(found))


def check_hosts(on):
    """Die unless every host in on is a host of this cluster."""
    there = sorted(nodes.members())
    for h in on:
        if h not in there:
            die(f"{h} is no host of this cluster (hosts: {', '.join(there)})")


def check_new(z, on, here):
    """A new net's zone z (on: --nodes, or None) checked: a VXLAN zone, or one to make; the hosts named
    are the cluster's and those of an existing zone; here (the net's gateway host) is one of them.
    Whether z will be made."""
    valid_id(z)
    if on is not None:
        check_hosts(on)
    found = zones().get(z)
    if found and found["type"] != "vxlan":
        t = found["type"]
        die(f"{z} is a {t} zone: pvelab makes nets in VXLAN zones only. For a {t} zone make the vnet in PVE "
            f"(Datacenter \u2192 SDN); pvelab create --net VNET takes it over")
    have = found["nodes"] if found else on
    if found and on is not None and set(on) != set(have or nodes.members()):
        die(f"{z} exists already on {hosts_text(have)}: pvelab zone set {z} --nodes ... changes that")
    if have and here not in have:
        die(f"{here} is not one of zone {z}'s hosts ({', '.join(have)}): a net's gateway is on the host that "
            f"makes it. Run it on one of them, or pvelab zone set {z} --nodes ...")
    if found and hand_tuned(z):
        warn(drift_line(z))
    return not found


def permission_line(z):
    """Where an admin lets users use zone z's nets (said once the zone is made)."""
    return (f"who may use {z}'s nets: Datacenter \u2192 Permissions \u2192 Add, path /sdn/zones/{z}, "
            "role PVESDNUser")

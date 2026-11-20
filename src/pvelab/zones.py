"""PVE's SDN zones, read from the cluster-wide zones.cfg: pvelab records nothing about them.

A zone's hosts are its `nodes`, or every host when it has none. A VXLAN zone's automatic peers are its
hosts' addresses; peers that differ from those (as a set) were tuned by hand and are left as they are."""

import re
import socket

from . import config, nodes
from .util import die

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


def addresses():
    """{host: IPv4} of every cluster host (from .members with several hosts); else this host's own:
    corosync's ring0 address, or the uplink's."""
    if nodes.multi():
        return {n: m["ip"] for n, m in nodes.members().items() if m["ip"]}
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


def drift_line(z, addrs=None):
    """One line on zone z's hand-tuned peers: what they are, what they would be, how to align them."""
    zs = zones()
    addrs = addresses() if addrs is None else addrs
    cur = ", ".join((zs.get(z) or {}).get("peers") or [])
    auto = ", ".join(auto_peers(z, addrs, zs))
    return (f"{z}: peers {cur} differ from its hosts' addresses ({auto}): left as they are "
            f"(pvelab zone set {z} --peers auto aligns them)")

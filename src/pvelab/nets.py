"""A net's name and CIDR.

Nets made before custom names/ranges have no name line and address 10.10.N.1/24: they are "netN" and
10.10.N.0/24. Newer nets carry "# pvelab name X" inside their interfaces block and any private CIDR.
The block is on the node with the net's gateway only; elsewhere an SDN net's name and range come from
its vnet labN (alias NAME; older nets "pvelab NAME") and that vnet's subnet in /etc/pve/sdn, which every
node shares. The gateway is the net's first address (.1) unless chosen when the net was made (--gateway):
the bridge's address in the block, or the SDN subnet's gateway.
"""

import contextlib
import functools
import ipaddress
import re

from . import config
from .args import valid_net
from . import ifaces
from .util import LabError, die, net_number, parallel, warn

NAME_LINE = "        # pvelab name {}\n"
_registered = {}                                  # n -> (name, cidr, gateway or None): nets this run is about to make


def default_cidr(n):
    return ipaddress.ip_network(f"{config.DHCP_NET}.0/24" if n == "dhcp" else f"10.10.{n}.0/24")


def register(n, name, cidr, gw=None):
    _registered[str(n)] = (name, cidr, gw)


def _block(n):
    return ifaces.blocks(config.read_text(config.IFACES)).get(str(n))


def sdn_vnets():
    """{vnet: (zone or None, alias or None)} of every vnet in the cluster-wide vnets.cfg, any zone."""
    out = {}
    for m in re.finditer(r"^vnet: (\S+)\n((?:[ \t]+.*\n?)*)", config.read_text(config.SDN_VNETS), re.M):
        vnet, body = m.groups()
        zone = re.search(r"^\s*zone (\S+)", body, re.M)
        alias = re.search(r"^\s*alias (.*?)\s*$", body, re.M)
        out[vnet] = (zone.group(1) if zone else None, alias.group(1) if alias else None)
    return out


def _sdn_nets():
    """{n: (name or None, cidr or None)} of pvelab's SDN vnets labN (zone SDN_ZONE), from the cluster-wide
    vnets.cfg (alias NAME, or "pvelab NAME") and subnets.cfg (the vnet's subnet)."""
    out = {}
    for vnet, (zone, alias) in sdn_vnets().items():
        num = re.fullmatch(r"lab(\d+)", vnet)
        if not num or zone != config.SDN_ZONE:
            continue
        name = re.fullmatch(r"(?:pvelab )?(\S+)", alias or "")
        out[num.group(1)] = (name.group(1) if name else None, None)
    for _, vnet, net in sdn_subnets():
        if vnet and (m := re.fullmatch(r"lab(\d+)", vnet)) and m.group(1) in out:
            out[m.group(1)] = (out[m.group(1)][0], net)
    return out


def _entry(n, name, net):
    """(name, cidr) with the defaults of a net made before custom names/ranges filled in."""
    return name or f"net{n}", net or default_cidr(n)


def _read(n):
    """(name or None, cidr or None): registered by this run, else from net n's block, else (another
    node's net) from its SDN vnet."""
    if n in _registered:
        return _registered[n][:2]
    b = _block(n)
    if b is None:
        return _sdn_nets().get(str(n), (None, None))
    return _parse(b)


def _address(b):
    """The ip_interface of a net's interfaces block (its address line: gateway/prefix), or None."""
    addr = re.search(r"^\s*address (\S+)$", b, re.M)
    try:
        return ipaddress.ip_interface(addr.group(1)) if addr else None
    except ValueError:
        return None


def _block_info(b):
    """(name or None, cidr or None, gateway or None) of a net's interfaces block: from its address line, or
    (a PVE vnet pvelab took over, whose gateway PVE sets) from the vnet's subnet in PVE."""
    name = re.search(r"^\s*# pvelab name (\S+)$", b, re.M)
    name = name.group(1) if name else None
    if iface := _address(b):
        return name, iface.network, iface.ip
    br = re.search(r"^iface (\S+)", b, re.M)
    sub = pve_subnet(br.group(1)) if br else None
    return (name, sub[0], sub[1]) if sub else (name, None, None)


def _parse(b):
    """(name or None, cidr or None) of a net's interfaces block."""
    return _block_info(b)[:2]


def cidr(n):
    n = str(n)
    return _entry(n, *_read(n))[1]


def gateway(n):
    """Net n's gateway: chosen when it was made (see the module text), else its first address."""
    n = str(n)
    if n in _registered:
        _, net, gw = _registered[n]
        net = net or default_cidr(n)
    elif (b := _block(n)) is not None:
        _, net, gw = _block_info(b)
        net = net or default_cidr(n)
    else:
        net = cidr(n)
        gw = next((e[3] for e in _subnet_entries() if e[1] == f"lab{n}"), None)
    return gw if gw is not None and usable(gw, net) else net.network_address + 1


def usable(a, net):
    """Whether a is an address of net that a host may have (not its own address nor its broadcast)."""
    return a in net and a not in (net.network_address, net.broadcast_address)


def parse_gateway(v, net):
    """--gateway V for a net with range net: an address in it (10.5.0.254), or .N (N after the net's own
    address: .254 is 10.5.0.254 in 10.5.0.0/24); not the net's own address nor its broadcast."""
    if re.fullmatch(r"\.\d+", v):
        k = int(v[1:])
        gw = net.network_address + k if k < net.num_addresses else None
    else:
        try:
            gw = ipaddress.ip_address(v)
        except ValueError:
            die(f"--gateway {v}: not an IPv4 address (like {net.network_address + 1}, or .254)")
    if gw is None or not usable(gw, net):
        die(f"--gateway {v}: not a usable address of {net} (from {net.network_address + 1} to "
            f"{net.broadcast_address - 1})")
    return gw


def net_name(n):
    n = str(n)
    if n == "dhcp":
        return f"{config.DHCP_BRIDGE}(dhcp)"
    return _entry(n, *_read(n))[0]


def vm_addresses(n):
    """The addresses VMs get on net n, in order (see addresses)."""
    return addresses(cidr(n), gateway(n))


def addresses(net, gw):
    """The addresses VMs get in range net with gateway gw, in order: from .11 up, without the gateway (it
    may be .254)."""
    a = net.network_address + config.IP_FIRST
    while a < net.broadcast_address:
        if a != gw:
            yield a
        a += 1


def all_nets():
    """{n: (name, cidr)}: the nets this run is about to make, the nets with a block here, then pvelab's SDN
    nets whose gateway is on another node."""
    text = config.read_text(config.IFACES)        # read and matched once for all the nets
    found, sdn = ifaces.blocks(text), _sdn_nets()
    out = {n: _entry(n, *v[:2]) for n, v in _registered.items()}
    for n in ifaces.our_nets(text):               # a begin line without its end: as on another node
        out.setdefault(n, _entry(n, *(_parse(found[n]) if n in found else sdn.get(n, (None, None)))))
    for n, v in sdn.items():
        out.setdefault(n, _entry(n, *v))
    return out


def resolve(v):
    """'30' / 'net30' -> '30'; a known net name -> its number; an unknown name -> None."""
    if net_number(v) is not None:
        return valid_net(v)
    return next((n for n, (name, _) in all_nets().items() if name == v), None)


def name_problem(name, n):
    if not name[:1].isalpha():
        return "a net name must start with a letter"
    if len(name) > 30:
        return "a net name has at most 30 characters (VM names add to it)"
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        return "a net name uses lowercase letters, digits and -"
    if name.endswith("-"):
        return "a net name cannot end in -"
    if name in ("dhcp", "auto", "all"):
        return f"'{name}' is reserved"
    if re.fullmatch(r"(vmbr|lab)\d+", name):
        return f"{name} looks like a bridge's name"
    if re.fullmatch(r"net\d+", name) and name != f"net{n}":
        return f"{name} looks like another net's number"
    for other, (other_name, _) in all_nets().items():
        if other != str(n) and other_name == name:
            return f"net {other} is already called {name}"
    return None


RFC1918 = tuple(ipaddress.ip_network(r) for r in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def parse_cidr(v):
    try:
        iface = ipaddress.IPv4Interface(v)
    except ValueError:
        die(f"{v!r} is not a CIDR like 192.168.50.0/24")
    if "/" not in v:
        die(f"{v!r} is not a CIDR like 192.168.50.0/24")
    net = iface.network
    if not any(net.subnet_of(r) for r in RFC1918):
        die(f"{v} is not a private range (use 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16)")
    if not 16 <= net.prefixlen <= 27:
        die(f"{v}: the prefix must be between /16 and /27")
    if iface.ip != net.network_address:
        die(f"{v} has host bits set; did you mean {net}?")
    return net


def overlap(net, others):
    """Why net overlaps one of others ({name: network}), or None."""
    for name, other in others.items():
        if net.overlaps(other):
            return f"{net} overlaps {name} ({other})"
    return None


def _route_lines(text):
    """[(network, device or None, connected)] of `ip -4 route` output: no default route, no host routes.
    connected: the kernel's route for an address of the host itself (proto kernel), e.g. its LAN."""
    out = []
    for line in text.splitlines():
        word = line.split()
        if not word or word[0] == "default":
            continue
        try:
            net = ipaddress.ip_network(word[0])
        except ValueError:
            continue
        if net.prefixlen == 32:
            continue
        dev = word[word.index("dev") + 1] if "dev" in word[:-1] else None
        out.append((net, dev, "proto" in word[:-1] and word[word.index("proto") + 1] == "kernel"))
    return out


_cached_read = None                               # inside cached_inputs(): _read_inputs, run at most once


@contextlib.contextmanager
def cached_inputs():
    """Inside: taken_ranges reads the hosts' routes and SDN subnets once, for every net."""
    global _cached_read
    _cached_read = functools.cache(lambda: _read_inputs())
    try:
        yield
    finally:
        _cached_read = None


def _subnet_entries():
    """[(id, vnet, network, gateway or None, snat)] of the cluster's SDN subnets (subnets.cfg)."""
    out = []
    for m in re.finditer(r"^subnet: (\S+)\n((?:[ \t]+.*\n?)*)", config.read_text(config.SDN_SUBNETS), re.M):
        ident, body = m.groups()
        tail = re.search(r"(\d+\.\d+\.\d+\.\d+)-(\d+)$", ident)
        vnet = re.search(r"^\s*vnet (\S+)", body, re.M)
        gw = re.search(r"^\s*gateway (\S+)", body, re.M)
        snat = re.search(r"^\s*snat\s+1\s*$", body, re.M) is not None
        if tail:
            try:
                out.append((ident, vnet.group(1) if vnet else None,
                            ipaddress.ip_network(f"{tail.group(1)}/{tail.group(2)}"),
                            ipaddress.ip_address(gw.group(1)) if gw else None, snat))
            except ValueError:
                pass
    return out


def pve_subnet(vnet):
    """(network, gateway or None, snat) of PVE vnet's subnet (the first, if it has several), or None."""
    return next((e[2:] for e in _subnet_entries() if e[1] == vnet), None)


def sdn_zones():
    """{zone: its type (simple, vxlan, vlan, evpn, ...)} from the cluster's zones.cfg."""
    return {z: t for t, z in re.findall(r"^(\w+): (\S+)", config.read_text(config.SDN_ZONES), re.M)}


def pve_vnets():
    """Every vnet in PVE's SDN config (any zone, whoever made it): [{vnet, zone, type, alias, cidr, gateway,
    snat}], in vnet order; cidr/gateway None without a subnet."""
    zones, out = sdn_zones(), []
    for vnet, (zone, alias) in sorted(sdn_vnets().items()):
        sub = pve_subnet(vnet) or (None, None, False)
        out.append({"vnet": vnet, "zone": zone, "type": zones.get(zone), "alias": alias, "cidr": sub[0],
                    "gateway": sub[1], "snat": sub[2]})
    return out


def pvelabs_own(vnet, zone):
    """Whether a vnet is one pvelab made (labN in its own zone)."""
    return zone == config.SDN_ZONE and re.fullmatch(r"lab\d+", vnet) is not None


def pve_gateway(n):
    """Whether PVE itself gives net n's vnet its gateway (and SNAT, if set): a taken-over vnet in a simple
    or EVPN zone. Then pvelab adds no address and no NAT of its own."""
    if not config.taken_over(n):
        return False
    zone = sdn_vnets().get(config.bridge(n), (None, None))[0]
    return sdn_zones().get(zone) in config.PVE_GATEWAY_ZONES


def free_vnets():
    """The PVE vnets that are neither pvelab's own nor taken over by it (rows of pve_vnets)."""
    taken = {config.bridge(n) for n in ifaces.our_nets()}
    return [v for v in pve_vnets() if not pvelabs_own(v["vnet"], v["zone"]) and v["vnet"] not in taken]


def free_vnet(v):
    """The free PVE vnet (pve_vnets row) whose id or alias is v, or None."""
    return next((r for r in free_vnets() if v in (r["vnet"], r["alias"])), None)


def sdn_subnets():
    """[(id, vnet, network)] of the cluster's SDN subnets (subnets.cfg)."""
    return [e[:3] for e in _subnet_entries()]


def _read_inputs():
    """(subnets [(id, vnet, network)], routes {node: [(network, device, connected)]}); warns about a host
    that does not answer."""
    from . import nodes
    subnets = sdn_subnets()
    online = [n for n, m in sorted(nodes.members().items()) if m["online"]]

    def routes_on(node):                          # '' when the host has no address or does not answer
        try:
            return nodes.run_on(node, "ip", "-4", "route", check=False, quiet=True, shared=False, timeout=20)
        except LabError:
            return ""
    texts = parallel(routes_on, online)
    routes = {}
    for node, text in texts.items():
        if text.strip():
            routes[node] = _route_lines(text)
        else:
            warn(f"routes on {node} not checked: no answer")
    return subnets, routes


def taken_ranges(n, net=None):
    """{label: network} of everything net n's CIDR (net: the range being checked, else n's) must stay clear
    of: vmbr1, the other pvelab nets, SDN subnets of other vnets and the routes of every online host (net
    n's own bridge excepted; n None: a net with no number yet, so nothing is its own). A route that covers
    the whole range and is not a network of the host's own (a VPN's 10.0.0.0/8) does not count: the net's
    own, narrower route wins on every host."""
    n = None if n is None else str(n)
    net = net or cidr(n)
    subnets, routes = (_cached_read or _read_inputs)()
    out = {f"{config.DHCP_BRIDGE} (dhcp)": default_cidr("dhcp")}     # a label no net name can have
    for other, (name, other_net) in all_nets().items():
        if other != n:
            out[name] = other_net
    own_bridges = {config.net_bridge(n, True), config.net_bridge(n, False), config.bridge(n)} if n else set()
    for ident, vnet, other in subnets:
        if vnet not in own_bridges:
            out[f"SDN subnet {ident}"] = other
    for node, found in routes.items():
        for other, dev, connected in found:
            if dev in own_bridges or (not connected and net.subnet_of(other) and other != net):
                continue
            label = f"route on {node}"
            out[label if label not in out else f"{label} ({other})"] = other
    return out


def cidr_problem(n, net):
    return overlap(net, taken_ranges(n, net))


def cidr_free(n, net):
    if why := cidr_problem(n, net):
        die(why)


def overlaps(n):
    """Lines saying what overlaps net n's CIDR now (empty: nothing does)."""
    name, net = net_name(n), cidr(n)
    return [f"{name} ({net}) overlaps {label} ({other}): VMs may not reach each other or the internet"
            for label, other in taken_ranges(n, net).items() if net.overlaps(other)]

"""A net's name and CIDR.

Nets made before custom names/ranges have no name line and address 10.10.N.1/24: they are "netN" and
10.10.N.0/24. Newer nets carry "# pvelab name X" inside their interfaces block and any private CIDR.
"""

import contextlib
import ipaddress
import re

from . import config
from .args import valid_net
from .ifaces import block_begin, block_end
from .util import die, parallel, warn

NAME_LINE = "        # pvelab name {}\n"
_registered = {}                                  # n -> (name, cidr): nets this run is about to make


def default_cidr(n):
    return ipaddress.ip_network(f"10.10.{10 if n == 'dhcp' else n}.0/24")


def register(n, name, cidr):
    _registered[str(n)] = (name, cidr)


def _block(n):
    text = config.read_text(config.IFACES)
    m = re.search(rf"^{re.escape(block_begin(n))}.*?^{re.escape(block_end(n).strip())}", text, re.M | re.S)
    return m.group(0) if m else None


def _read(n):
    """(name or None, cidr or None) from net n's block."""
    b = _block(n)
    if b is None:
        return None, None
    name = re.search(r"^\s*# pvelab name (\S+)$", b, re.M)
    addr = re.search(r"^\s*address (\S+)$", b, re.M)
    cidr = None
    if addr:
        try:
            cidr = ipaddress.ip_interface(addr.group(1)).network
        except ValueError:
            pass
    return (name.group(1) if name else None), cidr


def cidr(n):
    n = str(n)
    if n in _registered:
        return _registered[n][1]
    return _read(n)[1] or default_cidr(n)


def gateway(n):
    return cidr(n).network_address + 1


def net_name(n):
    n = str(n)
    if n == "dhcp":
        return f"{config.DHCP_BRIDGE}(dhcp)"
    if n in _registered:
        return _registered[n][0]
    return _read(n)[0] or f"net{n}"


def vm_addresses(n):
    net = cidr(n)
    a = net.network_address + config.IP_FIRST
    while a < net.broadcast_address:
        yield a
        a += 1


def all_nets():
    text = config.read_text(config.IFACES)
    out = {}
    for n in re.findall(r"^# lab\.sh net (\S+) begin", text, re.M):
        out[n] = (net_name(n), cidr(n))
    return out


def resolve(v):
    """'30' / 'net30' -> '30'; a known net name -> its number; an unknown name -> None."""
    if re.fullmatch(r"(net)?\d+", v):
        return valid_net(v)
    for n, (name, _) in all_nets().items():
        if name == v:
            return n
    for n, (name, _) in _registered.items():
        if name == v:
            return n
    return None


def name_problem(name, n):
    if not name[:1].isalpha():
        return "a net name must start with a letter"
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        return "a net name uses lowercase letters, digits and -"
    if name.endswith("-"):
        return "a net name cannot end in -"
    if name == "dhcp":
        return "'dhcp' is reserved"
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


def overlap(net, n, others):
    """Why net overlaps one of others ({name: network}), or None."""
    for name, other in others.items():
        if name == f"net{n}":
            continue
        if net.overlaps(other):
            return f"{net} overlaps {name} ({other})"
    return None


def _route_lines(text):
    """[(network, device or None)] of `ip -4 route` output: no default route, no host routes."""
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
        out.append((net, word[word.index("dev") + 1] if "dev" in word[:-1] else None))
    return out


def parse_routes(text):
    return [net for net, _ in _route_lines(text)]


_inputs_cache = None                              # None: not caching; [] : caching, nothing read yet


@contextlib.contextmanager
def cached_inputs():
    """Inside: taken_ranges reads the hosts' routes and SDN subnets once, for every net."""
    global _inputs_cache
    _inputs_cache = []
    try:
        yield
    finally:
        _inputs_cache = None


def _read_inputs():
    """(subnets [(id, vnet, network)], routes {node: [(network, device)]}); warns about a host that does not answer."""
    from . import nodes
    subnets = []
    for m in re.finditer(r"^subnet: (\S+)\n((?:[ \t]+.*\n?)*)", config.read_text(config.SDN_SUBNETS), re.M):
        ident, body = m.groups()
        tail = re.search(r"(\d+\.\d+\.\d+\.\d+)-(\d+)$", ident)
        vnet = re.search(r"^\s*vnet (\S+)", body, re.M)
        if tail:
            try:
                subnets.append((ident, vnet.group(1) if vnet else None,
                                ipaddress.ip_network(f"{tail.group(1)}/{tail.group(2)}")))
            except ValueError:
                pass
    online = [n for n, m in sorted(nodes.members().items()) if m["online"]]
    texts = parallel(lambda node: nodes.run_on(node, "ip", "-4", "route", check=False, quiet=True), online)
    routes = {}
    for node, text in texts.items():
        if text.strip():
            routes[node] = _route_lines(text)
        else:
            warn(f"routes on {node} not checked: no answer")
    return subnets, routes


def taken_ranges(n):
    """{label: network} of everything net n's CIDR must stay clear of: vmbr1, the other pvelab nets,
    SDN subnets of other vnets and the routes of every online host (net n's own bridge excepted)."""
    n = str(n)
    global _inputs_cache
    if _inputs_cache is None:
        subnets, routes = _read_inputs()
    else:
        if not _inputs_cache:
            _inputs_cache = list(_read_inputs())
        subnets, routes = _inputs_cache
    out = {config.DHCP_BRIDGE: default_cidr("dhcp")}
    for other, (name, net) in all_nets().items():
        if other != n:
            out[name] = net
    own_vnet, own_bridges = config.net_bridge(n, True), {config.net_bridge(n, True), config.net_bridge(n, False)}
    for ident, vnet, net in subnets:
        if vnet != own_vnet:
            out[f"SDN subnet {ident}"] = net
    for node, found in routes.items():
        for net, dev in found:
            if dev in own_bridges:
                continue
            label = f"route on {node}"
            out[label if label not in out else f"{label} ({net})"] = net
    return out


def cidr_problem(n, net):
    return overlap(net, n, taken_ranges(n))


def cidr_free(n, net):
    if why := cidr_problem(n, net):
        die(why)


def overlaps(n):
    """Lines saying what overlaps net n's CIDR now (empty: nothing does)."""
    name, net = net_name(n), cidr(n)
    return [f"{name} ({net}) overlaps {label} ({other}): VMs may not reach each other or the internet"
            for label, other in taken_ranges(n).items() if net.overlaps(other)]

"""A net's name and CIDR.

Nets made before custom names/ranges have no name line and address 10.10.N.1/24: they are "netN" and
10.10.N.0/24. Newer nets carry "# pvelab name X" inside their interfaces block and any private CIDR.
"""

import ipaddress
import re

from . import config
from .args import valid_net
from .ifaces import block_begin, block_end
from .util import die

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


def parse_cidr(v):
    try:
        iface = ipaddress.IPv4Interface(v)
    except ValueError:
        die(f"{v!r} is not a CIDR like 192.168.50.0/24")
    if "/" not in v:
        die(f"{v!r} is not a CIDR like 192.168.50.0/24")
    net = iface.network
    if not net.is_private:
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

"""pvelab forward: a port (TCP or UDP), a range of ports, or ping (ICMP echo) arriving on this host's uplink
forwarded to one VM on a pvelab net (DNAT). Which ones: the VM's record (forwards, as canonical() writes
them); the rules: its net's forward section in /etc/network/interfaces, made again whenever the net's VMs
change (networks.refresh_net), so they follow the VM's address and go away with it."""

import re
from collections import namedtuple

from . import registry
from .config import bridge, UPLINK
from .credentials import cred_rows
from .ifaces import has_section, iptables, write_section
from .util import listening_ports, warn

PROTOS = ("tcp", "udp")
ICMP = "icmp"
# proto tcp, udp or icmp; ports lo..hi of this host to the VM's to..to+(hi-lo) (icmp: all None)
Forward = namedtuple("Forward", "proto lo hi to")
SPEC = re.compile(r"(?P<lo>[0-9]{1,5})(?:-(?P<hi>[0-9]{1,5})|:(?P<to>[0-9]{1,5}))?(?:/(?P<proto>[a-z]+))?")


def parse(spec):
    """The Forward of spec: icmp (ping), PORT, PORT:VMPORT or LO-HI (the same ports on the VM), each with
    /tcp (the default) or /udp; ValueError says what is wrong."""
    if spec.lower() == ICMP:
        return Forward(ICMP, None, None, None)
    m = SPEC.fullmatch(spec.lower())
    if not m:
        raise ValueError(f"'{spec}' is no forward: PORT[/udp], PORT:VMPORT[/udp], LO-HI[/udp] or icmp")
    proto = m["proto"] or "tcp"
    if proto not in PROTOS:
        raise ValueError(f"'{spec}': tcp, udp or icmp, not {proto}")
    lo = int(m["lo"])
    hi = int(m["hi"]) if m["hi"] else lo
    to = int(m["to"]) if m["to"] else lo
    if not all(1 <= p <= 65535 for p in (lo, hi, to)):
        raise ValueError(f"'{spec}': ports are 1-65535")
    if hi < lo:
        raise ValueError(f"'{spec}': the range goes backwards")
    return Forward(proto, lo, hi, to)


def canonical(f):
    """How a Forward is written (records, messages): icmp, 51820/udp, 8080:80/tcp, 6000-6010/udp."""
    if f.proto == ICMP:
        return ICMP
    return f"{f.lo}" + (f"-{f.hi}" if f.hi != f.lo else "") + (f":{f.to}" if f.to != f.lo else "") + f"/{f.proto}"


def overlaps(a, b):
    """Whether a and b take the same packets: the same proto, and for tcp/udp a port in common."""
    return a.proto == b.proto and (a.proto == ICMP or (a.lo <= b.hi and b.lo <= a.hi))


def recorded(recs=None):
    """{vmid: [Forward]} of every VM whose record has forwards (recs: registry.records(), if read already)."""
    out = {}
    for v, rec in (registry.records() if recs is None else recs).items():
        found = []
        for spec in rec.get("forwards") or ():
            try:
                found.append(parse(spec))
            except ValueError as e:
                warn(f"VMID {v}'s record: {e} - left out")
        if found:
            out[v] = found
    return out


def taken_ports(proto):
    """The ports of this host pvelab forward forwards (proto tcp or udp), to any VM."""
    return {p for fs in recorded().values() for f in fs if f.proto == proto for p in range(f.lo, f.hi + 1)}


def busy_problem(f, busy):
    """Why Forward f's ports of this host can't be forwarded, or None. busy: {proto: listening_ports(proto)}."""
    if f.proto == ICMP:
        return None
    used = sorted(p for p in busy[f.proto] if f.lo <= p <= f.hi)
    return f"{f.proto.upper()} {', '.join(map(str, used))}: a service on this host listens there" if used else None


def rules(n, ip, f):
    """(FORWARD rule, nat PREROUTING rule) of Forward f to address ip on net n. Only DNATed packets may
    enter the bridge, as for ssh forwards (expose)."""
    head = f"-i {UPLINK} -p {f.proto}"
    if f.proto == ICMP:
        return (f"FORWARD {head} -o {bridge(n)} -d {ip} -m conntrack --ctstate DNAT -j ACCEPT",
                f"-t nat PREROUTING {head} --icmp-type echo-request -j DNAT --to-destination {ip}")
    if f.hi != f.lo:                         # a range: each port to the same port of the VM
        return (f"FORWARD {head} -o {bridge(n)} -d {ip} --dport {f.lo}:{f.hi} -m conntrack --ctstate DNAT -j ACCEPT",
                f"-t nat PREROUTING {head} --dport {f.lo}:{f.hi} -j DNAT --to-destination {ip}")
    return (f"FORWARD {head} -o {bridge(n)} -d {ip} --dport {f.to} -m conntrack --ctstate DNAT -j ACCEPT",
            f"-t nat PREROUTING {head} --dport {f.lo} -j DNAT --to-destination {ip}:{f.to}")


def set_forwards(n, on=True):
    """(Re)write net n's forward section from its VMs' records and apply it now; on=False removes it.
    Called by pvelab forward, and through refresh_net when VMs come and go - so a forward whose port a
    service of this host took meanwhile is left out with a warning instead of stopping the others."""
    if not on:
        return write_section("forward", n, [], [])
    rows, busy, fwd, nat = cred_rows(), None, [], []
    for v, fs in sorted(recorded().items()):
        r = rows.get(v)
        if not r or r["net"] != str(n) or r["ip"] == "-":
            continue
        for f in fs:
            if busy is None and f.proto != ICMP:
                busy = {p: listening_ports(p) for p in PROTOS}
            if why := busy_problem(f, busy):
                warn(f"{r['name']}: {canonical(f)} not forwarded ({why})")
                continue
            a, b = rules(n, r["ip"], f)
            fwd.append(a)
            nat.append(b)
    if not fwd and not has_section("forward", n):     # nothing to forward, nothing to take away
        return
    fwd_up, fwd_down = iptables(fwd, add="-I")
    nat_up, nat_down = iptables(nat)
    write_section("forward", n, fwd_up + nat_up, fwd_down + nat_down)

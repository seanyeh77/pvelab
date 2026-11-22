"""pvelab forward: a port (TCP or UDP), a range of ports, or ping (ICMP echo) arriving on this host's uplink
forwarded to one VM on a pvelab net (DNAT). Which ones: the VM's record (forwards, as canonical() writes
them); the rules: its net's forward section in /etc/network/interfaces, made again whenever the net's VMs
change (networks.refresh_net), so they follow the VM's address and go away with it."""

import re
from collections import namedtuple

from . import registry
from .config import bridge, SSH_PORT_BASE, UPLINK
from .helptext import bad_usage
from .ifaces import iptables, our_nets, write_section
from .util import LabError, warn

PROTOS = ("tcp", "udp")
# Ports of this host a forward never takes, whether or not ss shows a socket there (the kernel's own, like
# VXLAN's, are not listed): PVE's (ssh, web UI, SPICE proxy, rpcbind, corosync, live migration, which listens
# only while one runs), SDN's VXLAN, and the ssh
# ports pvelab expose gives VMIDs 100-9999. (lo, hi, how it is named)
RESERVED = {"tcp": [(22, 22, "22"), (85, 85, "85"), (111, 111, "111"), (3128, 3128, "3128"), (8006, 8006, "8006"),
                    (60000, 60050, "60000-60050 (live migration)"), (SSH_PORT_BASE + 100, SSH_PORT_BASE + 9999,
                     f"{SSH_PORT_BASE + 100}-{SSH_PORT_BASE + 9999} (pvelab expose's ssh ports)")],
            "udp": [(111, 111, "111"), (4789, 4789, "4789"), (5404, 5412, "5404-5412")]}
ICMP = "icmp"
# proto tcp, udp or icmp; ports lo..hi of this host to the VM's to..to+(hi-lo) (icmp: all None)
Forward = namedtuple("Forward", "proto lo hi to")
SPEC = re.compile(r"(?P<lo>[0-9]{1,5})(?:-(?P<hi>[0-9]{1,5})|:(?P<to>[0-9]{1,5}))?(?:/(?P<proto>[a-z]+))?")


def parse(spec):
    """The Forward of spec: icmp (ping), PORT, PORT:VMPORT or LO-HI (the same ports on the VM), each with
    /tcp (the default) or /udp; else bad_usage saying what is wrong."""
    if spec.lower() == ICMP:
        return Forward(ICMP, None, None, None)
    m = SPEC.fullmatch(spec.lower())
    if not m:
        bad_usage(f"'{spec}' is no forward: PORT[/udp], PORT:VMPORT[/udp], LO-HI[/udp] or icmp")
    proto = m["proto"] or "tcp"
    if proto not in PROTOS:
        bad_usage(f"'{spec}': tcp, udp or icmp, not {proto}")
    lo = int(m["lo"])
    hi = int(m["hi"]) if m["hi"] else lo
    to = int(m["to"]) if m["to"] else lo
    if not all(1 <= p <= 65535 for p in (lo, hi, to)):
        bad_usage(f"'{spec}': ports are 1-65535")
    if hi < lo:
        bad_usage(f"'{spec}': the range goes backwards")
    return Forward(proto, lo, hi, to)


def span(f):
    """This host's port(s) of a tcp/udp Forward: 51820, 6000-6010."""
    return f"{f.lo}-{f.hi}" if f.hi != f.lo else f"{f.lo}"


def canonical(f):
    """How a Forward is written (records, messages): icmp, 51820/udp, 8080:80/tcp, 6000-6010/udp."""
    return ICMP if f.proto == ICMP else span(f) + (f":{f.to}" if f.to != f.lo else "") + f"/{f.proto}"


def names(fs):
    return ", ".join(map(canonical, fs))


def describe(f):
    """A Forward for people: ping (ICMP echo), or its proto and this host's port(s) (UDP 51820)."""
    return "ping (ICMP echo)" if f.proto == ICMP else f"{f.proto.upper()} {span(f)}"


def target(ip, f):
    """Where Forward f sends to: the VM's address ip, with its port(s)."""
    return ip if f.proto == ICMP else f"{ip}:{span(f) if f.hi != f.lo else f.to}"


def overlaps(a, b):
    """Whether a and b take the same packets: the same proto, and for tcp/udp a port in common."""
    return a.proto == b.proto and (a.proto == ICMP or (a.lo <= b.hi and b.lo <= a.hi))


_warned = set()


def warn_once(text):
    """warn, once per run: one refresh reads the records several times."""
    if text not in _warned:
        _warned.add(text)
        warn(text)


def recorded(recs=None):
    """{vmid: [Forward]} of every VM whose record has forwards. recs: {vmid: record}, if read already
    (default: the records with forwards)."""
    out = {}
    for v, rec in (registry.records(has="forwards") if recs is None else recs).items():
        specs, found = rec.get("forwards") or [], []
        if not isinstance(specs, list):
            warn_once(f"VMID {v}'s record: forwards is no list ({specs!r}) - left out")
            specs = []
        for spec in specs:
            try:
                found.append(parse(str(spec)))
            except LabError as e:
                warn_once(f"VMID {v}'s record: {e} - left out")
        if found:
            out[v] = found
    return out


def here(recs):
    """The records of recs whose VM's net has its gateway (and its rules) on this host: the forwards that
    share this host's uplink."""
    ours = set(our_nets())
    return {v: r for v, r in recs.items() if str(r.get("net")) in ours}


def taken_ports(proto):
    """The ports of this host pvelab forward forwards (proto tcp or udp), to any VM of a net with its
    gateway here."""
    found = recorded(here(registry.records(has="forwards")))
    return {p for fs in found.values() for f in fs if f.proto == proto for p in range(f.lo, f.hi + 1)}


def on_net(n):
    """Whether a record of a VM on net n has forwards (the registry file alone: no VM config read)."""
    return any(r.get("forwards") and str(r.get("net")) == str(n) for r in registry.load()["vms"].values())


def busy_problem(f, busy):
    """Why tcp/udp Forward f's ports of this host can't be forwarded, or None. busy: the ports a service of
    this host listens on (listening_ports(f.proto))."""
    if label := next((label for lo, hi, label in RESERVED[f.proto] if overlaps(f, Forward(f.proto, lo, hi, lo))), None):
        return f"{f.proto.upper()} {label}: this host's own, never forwarded"
    used = sorted(p for p in busy if f.lo <= p <= f.hi)
    return f"{f.proto.upper()} {', '.join(map(str, used))}: a service on this host listens there" if used else None


def rules(n, ip, f):
    """(FORWARD rule, nat PREROUTING rule) of Forward f to address ip on net n. Only DNATed packets may
    enter the bridge, as for ssh forwards (expose); only packets to this host itself are DNATed, not ones
    bridged on the uplink to other machines."""
    if f.proto == ICMP:
        accept, match, to = "", "--icmp-type echo-request", ip
    elif f.hi != f.lo:                       # a range: each port to the same port of the VM
        accept, match, to = f" --dport {f.lo}:{f.hi}", f"--dport {f.lo}:{f.hi}", ip
    else:
        accept, match, to = f" --dport {f.to}", f"--dport {f.lo}", f"{ip}:{f.to}"
    head = f"-i {UPLINK} -p {f.proto}"
    return (f"FORWARD {head} -o {bridge(n)} -d {ip}{accept} -m conntrack --ctstate DNAT -j ACCEPT",
            f"-t nat PREROUTING {head} -m addrtype --dst-type LOCAL {match} -j DNAT --to-destination {to}")


def set_forwards(n, on=True):
    """(Re)write net n's forward section from its VMs' records and apply it now; on=False removes it.
    Called by pvelab forward, and through refresh_net when VMs come and go. A VM blocked from the internet
    keeps them, but its block rules drop its side of what they bring in (blocks.block_rules). Ports are checked when a
    forward is added (pvelab forward), not here: a service started on one later only stops being reachable
    from outside, as the forward takes what comes in first."""
    if not on:
        return write_section("forward", n, [], [])
    mine = {v: r for v, r in registry.records(has="forwards").items() if str(r.get("net")) == str(n) and r.get("ip")}
    pairs = [rules(n, mine[v]["ip"], f) for v, fs in sorted(recorded(mine).items()) for f in fs]
    fwd_up, fwd_down = iptables([a for a, _ in pairs], add="-I")
    nat_up, nat_down = iptables([b for _, b in pairs])
    write_section("forward", n, fwd_up + nat_up, fwd_down + nat_down)

"""pvelab forward [VM [SPEC]... [--off]]: a port of this host (TCP or UDP), a range of them, or ping (icmp)
forwarded to one VM; without a VM, every VM's forwards."""

from .. import legacy, registry
from ..config import UPLINK
from ..expose import exposure, ssh_port
from ..forwards import busy_problem, canonical, describe, ICMP, names, overlaps, parse, recorded, set_forwards, target
from ..helptext import bad_usage
from ..inventory import resolve
from ..networks import require_rules_here
from ..ui import Plan
from ..util import die, listening_ports

USAGE = "pvelab forward [VM [PORT[/udp] | PORT:VMPORT[/udp] | LO-HI[/udp] | icmp]... [--off]]"


def parse_args(args):
    """(VM or None, [Forward] without repeats, off) of forward's arguments, or bad_usage."""
    vm, specs, off = None, [], False
    for a in args:
        if a == "--off":
            off = True
        elif a.startswith("-"):
            bad_usage(f"unexpected '{a}' - {USAGE}")
        elif vm is None:
            vm = a
        else:
            specs.append(parse(a))
    if vm is None and off:
        bad_usage(f"which VM's forwards? {USAGE}")
    return vm, list(dict.fromkeys(specs)), off


def show(recs, found):
    """Print the forwards found ({vmid: [Forward]}); recs: the records (names, addresses)."""
    for v, fs in found.items():
        for f in fs:
            print(f"{recs[v].get('name', v):<18} {canonical(f):<16} -> {target(recs[v].get('ip', '-'), f)}")


def conflict(f, found, recs, ssh, busy):
    """Why Forward f can't be added, or None. found: recorded() with this run's earlier new ones added;
    ssh: {port: VMID} of the ssh forwards (expose); busy: {proto: listening_ports}, filled as needed."""
    def who(v):
        return recs.get(v, {}).get("name", v)
    for v, fs in found.items():
        for g in fs:
            if overlaps(f, g):
                return (f"icmp: ping goes to {who(v)} already (one VM only)" if f.proto == ICMP
                        else f"{canonical(f)}: {who(v)} has {canonical(g)} already")
    if f.proto == ICMP:
        return None
    if f.proto == "tcp" and (hit := sorted(p for p in ssh if f.lo <= p <= f.hi)):
        return f"{canonical(f)}: TCP {hit[0]} is {who(ssh[hit[0]])}'s ssh port (pvelab expose)"
    if f.proto not in busy:
        busy[f.proto] = listening_ports(f.proto)
    return f"{canonical(f)}: {why}" if (why := busy_problem(f, busy[f.proto])) else None


def cmd_forward(args, dry=False):
    vm, specs, off = parse_args(args)
    recs = registry.records()
    found = recorded(recs)
    if vm is None:
        return show(recs, found) if found else print("no VM has port forwards (pvelab forward VM PORT[/udp] adds one)")
    vmid = resolve(vm)
    if vmid not in recs:
        die(f"{vm} has no pvelab record (address, net): pvelab migrate records it")
    rec, have = recs[vmid], found.get(vmid, [])
    name, n, ip = rec.get("name", vm), str(rec.get("net")), rec.get("ip")
    if not (specs or off) or (off and not have):
        return show(recs, {vmid: have}) if have else print(f"{name} has no port forwards")
    require_rules_here(n)
    plan = Plan()
    if off:
        if missing := [f for f in specs if f not in have]:
            die(f"{name} does not forward {names(missing)} (its forwards: {names(have)})")
        gone = specs or have
        keep = [f for f in have if f not in gone]
        plan.do(f"stop forwarding {names(gone)} to {name}")
        done = f"{name}: {names(gone)} no longer forwarded"
    else:
        if not ip:
            die(f"{name} has no address yet: pvelab forward needs the one it has on its net")
        if not (new := [f for f in specs if f not in have]):
            print(f"{name} forwards {names(specs)} already")
            return
        ssh, seen, busy = {ssh_port(v): v for v in exposure()[1]}, dict(found), {}
        for f in new:
            if why := conflict(f, seen, recs, ssh, busy):
                die(why)
            seen[vmid] = [*seen.get(vmid, []), f]
        keep = have + new
        for f in new:
            plan.do(f"forward {describe(f)} arriving on this host ({UPLINK}) to {name} ({target(ip, f)})")
        if any(f.proto == ICMP for f in new):
            plan.warn(f"this host stops answering ping on {UPLINK}: {name} answers it")
        done = (f"{name}: {names(new)} forwarded. Allow {', '.join(map(describe, new))} in the cloud security "
                "group / firewall in front of this host.")
    if not plan.go(dry):
        return
    legacy.write({vmid: {"forwards": [canonical(f) for f in keep] or None}})
    set_forwards(n)
    print(done)

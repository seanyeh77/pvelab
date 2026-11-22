"""pvelab forward [VM [SPEC]... [--off]]: a port of this host (TCP or UDP), a range of them, or ping (icmp)
forwarded to one VM; without a VM, every VM's forwards."""

from .. import legacy, registry
from ..config import UPLINK
from ..expose import exposure, ssh_port
from ..forwards import busy_problem, canonical, describe, here, ICMP, names, overlaps, parse, recorded, set_forwards, target
from ..helptext import bad_usage
from ..inventory import read_conf, resolve
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
            print(f"{recs[v].get('name', v):<18} {canonical(f):<16} -> {target(recs[v].get('ip') or '-', f)}")


def conflict(f, found, recs, ssh, busy):
    """Why Forward f can't be added, or None. found: the forwards on this host's uplink (here), with this run's
    earlier new ones added;
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
    recs = registry.records(has="forwards")     # the VMs with forwards: names for show and conflict
    found = recorded(recs, say=True)
    if vm is None:
        return show(recs, found) if found else print("no VM has port forwards (pvelab forward VM PORT[/udp] adds one)")
    vmid = resolve(vm)
    if not (rec := registry.lookup(vmid, read_conf(vmid))):
        die(f"{vm} has no pvelab record (address, net): pvelab migrate records it")
    recs[vmid], have = rec, found.get(vmid, [])
    name, ip = rec.get("name", vm), rec.get("ip")
    if not rec.get("net"):
        die(f"{name} is on no pvelab net (its record has none): pvelab forward needs one")
    n = str(rec["net"])
    if not (specs or off) or (off and not specs and not have):
        return show(recs, {vmid: have}) if have else print(f"{name} has no port forwards")
    rules_here = require_rules_here(n, gone_ok=off)      # --off of a net gone (or plain): only its record changes
    plan = Plan()
    if off:
        if missing := [f for f in specs if f not in have]:
            die(f"{name} does not forward {names(missing)}"
                + (f" (its forwards: {names(have)})" if have else " (it has none)"))
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
        ssh, busy = {ssh_port(v): v for v in exposure()[1]}, {}
        seen = {v: list(fs) for v, fs in found.items() if v in here(recs)}
        for f in new:
            if why := conflict(f, seen, recs, ssh, busy):
                die(why)
            seen.setdefault(vmid, []).append(f)
        keep = have + new
        for f in new:
            plan.do(f"forward {describe(f)} arriving on this host ({UPLINK}) to {name} ({target(ip, f)})")
        if "internet" in (rec.get("blocks") or ()):
            plan.warn(f"{name} is blocked from the internet: what its forwards bring in gets no answer until "
                      f"pvelab unblock internet {name}")
        if any(f.proto == ICMP for f in new):
            plan.warn(f"this host stops answering ping on {UPLINK} - from the internet and from its LAN too (the other "
                      f"PVE hosts, monitoring): {name} answers it, and nothing does while {name} is off")
        done = (f"{name}: {names(new)} forwarded. Allow {', '.join(map(describe, new))} in the cloud security "
                "group / firewall in front of this host.")
    if not plan.go(dry):
        return
    specs_kept = [canonical(f) for f in keep] or None
    if rules_here:                           # the rules first: recorded is what they do
        set_forwards(n, recs={**recs, vmid: {**rec, "forwards": specs_kept}})
    legacy.write({vmid: {"forwards": specs_kept}})
    print(done)

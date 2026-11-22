"""pvelab forward [VM [SPEC]... [--off]]: a port of this host (TCP or UDP), a range of them, or ping (icmp)
forwarded to one VM; without a VM, every VM's forwards."""

from .. import legacy, registry
from ..config import SSH_PORT_BASE, UPLINK
from ..blocks import blocks_of
from ..expose import ssh_ports_here
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


def conflict(f, vmid, have, found, recs, ssh, busy):
    """Why Forward f to VM vmid can't be added, or None. have: its forwards; found: the forwards on this host's
    uplink (here), with this run's earlier new ones added; ssh: {port: VM name} of the ssh ports expose gives
    this host's VMs (ssh_ports_here); busy: {proto: listening_ports}, filled as needed."""
    for v, fs in found.items():
        for g in fs:
            if not overlaps(f, g):
                continue
            if v == vmid and g not in have:      # given earlier in this run
                return f"{canonical(f)} and {canonical(g)} overlap: give one of them"
            who = recs.get(v, {}).get("name", v)
            taken = f"ping goes to {who} already (one VM only)" if f.proto == ICMP else f"{who} has {canonical(g)} already"
            return f"{canonical(f)}: {taken} - pvelab forward {who} {canonical(g)} --off first" \
                + ("" if v == vmid or f.proto == ICMP else ", or another port")
    if f.proto == ICMP:
        return None
    if f.proto == "tcp" and (hit := min((p for p in ssh if f.lo <= p <= f.hi), default=None)):
        return f"{canonical(f)}: TCP {hit} is {ssh[hit]}'s ssh port (pvelab expose) - another port"
    if f.proto not in busy:
        busy[f.proto] = listening_ports(f.proto)
    return f"{canonical(f)}: {why} - another port" if (why := busy_problem(f, busy[f.proto])) else None


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
    if off:                                  # a spec names this host's ports: 8080 is 8080:80 too
        gone = [g for g in have if not specs or any((g.proto, g.lo, g.hi) == (f.proto, f.lo, f.hi) for f in specs)]
        if missing := [f for f in specs if not any((g.proto, g.lo, g.hi) == (f.proto, f.lo, f.hi) for g in have)]:
            die(f"{name} does not forward {names(missing)}"
                + (f" (its forwards: {names(have)})" if have else " (it has none)"))
        keep = [f for f in have if f not in gone]
        plan.do(f"stop forwarding {names(gone)} to {name}")
        done = f"{name}: {names(gone)} no longer forwarded"
    else:
        if not ip:
            die(f"{name} has no address yet: pvelab forward needs the one it has on its net")
        if not (new := [f for f in specs if f not in have]):
            print(f"{name} forwards {names(specs)} already")
            return
        high = any(f.proto == "tcp" and f.hi > SSH_PORT_BASE + 9999 for f in new)     # lower: RESERVED_PORTS'
        ssh, busy = ssh_ports_here() if high else {}, {}
        mine = here(recs)
        seen = {v: list(fs) for v, fs in found.items() if v in mine}
        for f in new:
            if why := conflict(f, vmid, have, seen, recs, ssh, busy):
                die(why)
            seen.setdefault(vmid, []).append(f)
        keep = have + new
        for f in new:
            plan.do(f"forward {describe(f)} arriving on this host ({UPLINK}) to {name} ({target(ip, f)})")
        if "internet" in blocks_of(rec):
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
    try:
        if rules_here:                       # the rules first: recorded is what they do
            set_forwards(n, recs={**recs, vmid: {**rec, "forwards": specs_kept}})
        legacy.write({vmid: {"forwards": specs_kept}})
    except BaseException:                    # failed, or not recorded (Ctrl-C): the rules as the records still say
        if rules_here:
            set_forwards(n)
        raise
    print(done)

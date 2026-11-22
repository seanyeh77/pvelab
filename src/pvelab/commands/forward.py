"""pvelab forward [VM [SPEC]... [--off]]: a port of this host (TCP or UDP), a range of them, or ping (icmp)
forwarded to one VM; without a VM, every VM's forwards."""

from .. import legacy
from ..config import UPLINK
from ..credentials import cred_rows
from ..expose import exposure, ssh_port
from ..forwards import busy_problem, canonical, ICMP, overlaps, parse, recorded, set_forwards
from ..helptext import bad_usage
from ..inventory import resolve
from ..ui import Plan
from ..util import die, listening_ports
from .access import require_rules_here

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
            try:
                specs.append(parse(a))
            except ValueError as e:
                bad_usage(str(e))
    if vm is None and off:
        bad_usage(f"which VM's forwards? {USAGE}")
    return vm, list(dict.fromkeys(specs)), off


def target(ip, f):
    """Where Forward f sends to: the VM's address, and port(s)."""
    return ip if f.proto == ICMP else f"{ip}:{f.lo}-{f.hi}" if f.hi != f.lo else f"{ip}:{f.to}"


def show(only=None):
    """Print every VM's forwards (only: that VM's)."""
    rows, found = cred_rows(), recorded()
    if only is not None:
        found = {only: found[only]} if only in found else {}
    if not found:
        print((f"{rows[only]['name']} has" if only is not None else "no VM has") + " no port forwards"
              + " (pvelab forward VM PORT[/udp] adds one)")
        return
    for v, fs in found.items():
        r = rows.get(v, {"name": str(v), "ip": "-"})
        for f in fs:
            print(f"{r['name']:<18} {canonical(f):<16} -> {target(r['ip'], f)}")


def conflict(f, vmid, found, rows, ssh):
    """Why Forward f to VM vmid can't be added, or None. found: recorded() with the other new ones of this
    run added; ssh: {port: VMID} of the ssh forwards (expose)."""
    for v, fs in found.items():
        for g in fs:
            if overlaps(f, g) and (v, g) != (vmid, f):
                who = rows.get(v, {}).get("name", str(v))
                return (f"{canonical(f)}: ping goes to {who} already (one VM only)" if f.proto == ICMP
                        else f"{canonical(f)}: {who} has {canonical(g)} already")
    if f.proto == ICMP:
        return None
    if f.proto == "tcp" and (hit := sorted(p for p in ssh if f.lo <= p <= f.hi)):
        who = rows.get(ssh[hit[0]], {}).get("name", str(ssh[hit[0]]))
        return f"{canonical(f)}: TCP {hit[0]} is {who}'s ssh port (pvelab expose)"
    if why := busy_problem(f, {f.proto: listening_ports(f.proto)}):
        return f"{canonical(f)}: {why}"
    return None


def allow_line(fs):
    """What to allow in front of this host for Forwards fs."""
    parts = [("ICMP echo (ping)" if f.proto == ICMP else f"{f.proto.upper()} {canonical(f).split('/')[0]}")
             for f in fs]
    return f"Allow {', '.join(parts)} in the cloud security group / firewall in front of this host."


def cmd_forward(args, dry=False):
    vm, specs, off = parse_args(args)
    if vm is None:
        return show()
    vmid, rows = resolve(vm), cred_rows()
    if vmid not in rows:
        die(f"{vm} has no pvelab record (address, net): pvelab migrate records it")
    r, found = rows[vmid], recorded()
    name, n, have = r["name"], r["net"], found.get(vmid, [])
    if not specs and not off:
        return show(vmid)
    require_rules_here(n)
    plan = Plan()
    if off:
        if missing := [f for f in specs if f not in have]:
            die(f"{name} does not forward {', '.join(map(canonical, missing))}"
                + (f" (its forwards: {', '.join(map(canonical, have))})" if have else " (it has none)"))
        gone = specs or have
        if not gone:
            print(f"{name} has no port forwards")
            return
        keep = [f for f in have if f not in gone]
        plan.do(f"stop forwarding {', '.join(map(canonical, gone))} to {name}")
    else:
        if r["ip"] == "-":
            die(f"{name} has no address yet: pvelab forward needs the one it has on its net")
        new = [f for f in specs if f not in have]
        if not new:
            print(f"{name} forwards {', '.join(map(canonical, specs))} already")
            return
        ssh, seen = {ssh_port(v): v for v in exposure()[1]}, {**found}
        for f in new:
            if why := conflict(f, vmid, seen, rows, ssh):
                die(why)
            seen[vmid] = [*seen.get(vmid, []), f]
        keep = have + new
        for f in new:
            plan.do(f"forward {'ping (ICMP echo)' if f.proto == ICMP else canonical(f)} arriving on this host "
                    f"({UPLINK}) to {name} ({target(r['ip'], f)})")
        if any(f.proto == ICMP for f in new):
            plan.warn(f"this host stops answering ping on {UPLINK}: {name} answers it")
    if not plan.go(dry):
        return
    legacy.write({vmid: {"forwards": [canonical(f) for f in keep] or None}})
    set_forwards(n)
    if off:
        print(f"{name}: {', '.join(map(canonical, gone))} no longer forwarded")
    else:
        print(f"{name}: {', '.join(map(canonical, new))} forwarded. {allow_line(new)}")

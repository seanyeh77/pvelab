"""pvelab reach [VM [--off]]: a VM that reaches the VMs of every other net of its SDN zone (a VPN server);
without a VM, the VMs that do."""

from .. import legacy, registry
from ..helptext import bad_usage
from ..inventory import read_conf, resolve
from ..nets import net_name
from ..networks import require_rules_here
from ..reaches import KIND, set_reach, zone_nets, zone_of
from ..ui import Plan
from ..util import die

USAGE = "pvelab reach [VM [--off]]"


def parse_args(args):
    """(VM or None, off) of reach's arguments, or bad_usage."""
    vm, off = None, False
    for a in args:
        if a == "--off":
            off = True
        elif a.startswith("-") or vm is not None:
            bad_usage(f"unexpected '{a}' - {USAGE}")
        else:
            vm = a
    if vm is None and off:
        bad_usage(f"which VM? {USAGE}")
    return vm, off


def reached(n):
    """The names of the nets a VM of net n reaches (its zone's others here), or ""."""
    z = zone_of(n)
    return ", ".join(net_name(m) for m in zone_nets(z, n)) if z else ""


def show(recs):
    for v, r in sorted(recs.items()):
        n = str(r.get("net"))
        print(f"{r.get('name', v):<18} {net_name(n):<12} zone {zone_of(n) or '-':<10} -> {reached(n) or '(no other net here yet)'}")


def cmd_reach(args, dry=False):
    vm, off = parse_args(args)
    if vm is None:
        recs = registry.records(has=KIND)
        return show(recs) if recs else print("no VM reaches other nets (pvelab reach VM: every net of its zone)")
    vmid = resolve(vm)
    if not (rec := registry.lookup(vmid, read_conf(vmid))):
        die(f"{vm} has no pvelab record (address, net): pvelab migrate records it")
    name, ip, on = rec.get("name", vm), rec.get("ip"), bool(rec.get(KIND))
    if not rec.get("net"):
        die(f"{name} is on no pvelab net (its record has none): pvelab reach needs one")
    n = str(rec["net"])
    rules_here = require_rules_here(n, gone_ok=off)      # --off of a net gone (or plain): only its record changes
    plan = Plan()
    if off:
        if not on:
            print(f"{name} does not reach other nets")
            return
        plan.do(f"{name} no longer reaches the other nets of its zone")
        done = f"{name}: no longer reaches other nets"
    else:
        if not ip:
            die(f"{name} has no address yet: pvelab reach needs the one it has on its net")
        if not (z := zone_of(n)):
            die(f"{net_name(n)} is in no SDN zone (a bridge of this host only): there are no nets of its zone to reach")
        others = zone_nets(z, n)
        if on:                               # the rules again: a zone changed in PVE's web UI, say
            set_reach(n)
            print(f"{name} reaches {reached(n) or 'no other net yet'} already (zone {z}); its rules made again")
            return
        plan.do(f"{name} ({ip}) reaches the VMs of every other net of zone {z} with its gateway here: "
                + (", ".join(net_name(m) for m in others) or "none yet") + " - and the ones made later")
        plan.warn(f"only {name}'s own connections cross: those nets still do not reach {name} (nor each other), "
                  f"and owners kept apart there are not kept apart from {name}")
        done = f"{name}: reaches the other nets of zone {z}"
    if not plan.go(dry):
        return
    value = None if off else True
    try:
        if rules_here:                       # the rules first: recorded is what they do
            set_reach(n, recs={**registry.records(has=KIND), vmid: {**rec, KIND: value}})
        legacy.write({vmid: {KIND: value}})
    except BaseException:                    # failed, or not recorded (Ctrl-C): the rules as the records still say
        if rules_here:
            set_reach(n)
        raise
    print(done)

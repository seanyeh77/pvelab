"""pvelab net [list] | net create NAME [--cidr CIDR] [--gateway ADDR] [--no-isolate] | net set NAME
[--cidr CIDR] [--gateway ADDR] | net rename OLD NEW | net remove NAME
A network on its own: made before its VMs, listed, changed, renamed, removed once it has none."""

import itertools

from ..args import opt
from ..config import DEFAULT_TEMPLATE
from ..config import bridge, in_sdn, is_plain, is_sdn, taken_over
from .. import netcheck, nets, sdn, ui
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours
from ..inventory import vm_names
from ..isolation import is_isolated
from ..networks import foreign_use, guests_on, host_dns, is_new_net, missing, remove_bridge, repair_rules
from ..nodes import require_quorum, this_node
from ..readdress import local_net, net_rename, net_set
from ..ui import paint, Plan
from ..util import die, LabError, ok
from .create import pick_net, prepare_net

USAGE = ("net takes: list, create NAME [--cidr CIDR] [--gateway ADDR] [--no-isolate], "
         "set NAME [--cidr CIDR] [--gateway ADDR] [--dry-run], rename OLD NEW [--rename-vms] [--dry-run], remove NAME "
         "[--dry-run], check NAME [--template T], or fix NAME")


def check_bridge(v):
    """--net-like V for net check -> (the net, None for a bridge that is no net; the bridge to try; None for
    DHCP or a pvelab net's fixed address as netcheck.check's static: its VMs get theirs from cloud-init,
    not DHCP), or die."""
    n = nets.resolve(v) if v != "dhcp" else "dhcp"
    if n is None and nets.host_bridge(v):    # a PVE vnet not taken over yet: as an external bridge
        return None, v, None
    if n is None:
        die(f"no net or bridge called '{v}' on this host (pvelab net list shows the nets)")
    br = bridge(n)
    if not nets.host_bridge(br):
        die(f"{nets.net_name(n)}: its bridge {br} is not on this host (a new net, or its gateway is elsewhere)")
    if is_plain(n):
        return n, br, None
    return n, br, (check_address(n), nets.cidr(n).prefixlen, str(nets.gateway(n)), [host_dns()])


def check_address(n, tries=5):
    """An address of net n for the check: from the top of its range (VMs get the first ones), none a VM
    holds (credentials) or that answers ping (one of an admin's), the gateway left out."""
    used = {r.get("ip") for r in cred_rows().values() if r.get("net") == n}
    free = (str(a) for a in reversed(list(nets.vm_addresses(n))) if str(a) not in used)
    for a in itertools.islice(free, tries):
        if not ok("ping", "-c", "1", "-W", "1", a):
            return a
    die(f"{nets.net_name(n)}: no free address to try from (the top {tries} answer ping)")


def cmd_net_check(args):
    """net check NAME [--template T]: does a VM on that net's bridge get a network (as a new VM would)?"""
    tpl, rest = DEFAULT_TEMPLATE, []
    it = iter(args[1:])
    for a in it:
        if (v := opt(a, it, "--template")) is not None:
            tpl = v
        else:
            rest.append(a)
    if len(rest) != 1 or rest[0].startswith("-"):
        bad_usage("pvelab net check NAME [--template T] (a net, or a bridge of this host such as vmbr0)")
    (n, br, static), mirror = check_bridge(rest[0]), netcheck.mirror_of(tpl)
    gone, why = None, None
    if n and bridge_is_ours(n):              # pvelab's own rules for its net
        try:
            gone = missing(n)
        except LabError as e:                # iptables could not tell: the network is tried still
            why = str(e)
    steps = netcheck.check(br, mirror, static)
    rules = [("rules", False, why)] if why else [] if gone is None else \
        [("rules", not gone, f"missing: {'; '.join(gone)}" if gone else "all in place")]
    for step, good, detail in rules + steps:
        print(f"  {paint('✔', 'green') if good else paint('✘', 'red')} {step:<9} {detail}")
    msg = netcheck.no_network(br, steps)
    if why:
        die((f"{msg}; " if msg else "") + why)
    if gone:
        die((f"{msg}; " if msg else "") + f"{nets.net_name(n)}: missing: {'; '.join(gone)} - to put back in "
            f"order: pvelab net fix {rest[0]}")
    if msg:
        die(msg)
    print(paint(f"a VM on {br} gets a network: {'its fixed address' if static else 'DHCP'}, and {mirror} for its first boot", "green"))


def cmd_net_fix(args):
    """net fix NAME: a pvelab net's own iptables rules, all of them again in order when one is missing."""
    if len(args) != 2 or args[1].startswith("-"):
        bad_usage("pvelab net fix NAME")
    n = nets.resolve(args[1])
    if n is None or not bridge_is_ours(n):
        die(f"'{args[1]}' is not pvelab's net on this host: only its own nets have rules to put back "
            "(pvelab net list shows them)")
    if not repair_rules(n):
        print(f"{nets.net_name(n)}: its rules are all in place")


def cmd_net(args):
    """net, net list: every network."""
    if args and args[0] != "list" or len(args) > 1:
        bad_usage(USAGE)
    net_list()


def nat_of(v):
    """Who masquerades a PVE vnet (pve_vnets row): PVE, or nobody (-)."""
    return "PVE" if nets.pve_snat(v) else "-"


def net_list():
    """Every network: pvelab's (its own and the PVE vnets it took over) and every other vnet in PVE's SDN
    config. VMs: the guests with a card on it, from their PVE configs."""
    pve = {v["vnet"]: v for v in nets.pve_vnets()}
    rows, free = [], nets.free_vnets()
    for n, (name, net) in sorted(nets.all_nets().items(), key=lambda kv: int(kv[0])):
        br = bridge(n)
        v = pve.get(br) if in_sdn(n) else None
        nat = nat_of(v) if taken_over(n) else "pvelab"
        apart = ("kept apart" if is_isolated(n) else "reach each other") if bridge_is_ours(n) else "-"
        rows.append((name, br, (v or {}).get("zone") or "-", net, nets.gateway(n), nat, len(guests_on(br)), apart))
    for v in free:
        rows.append((v["alias"] or v["vnet"], v["vnet"], v["zone"] or "-", v["cidr"] or "-", v["gateway"] or "-",
                     nat_of(v), len(guests_on(v["vnet"])), "not pvelab's yet" if nets.takeable(v) else "its own"))
    if not rows:
        print("No networks yet. Make one: pvelab net create NAME --cidr 10.5.0.0/24 --gateway .254")
        return
    fmt = "{:<16} {:<9} {:<9} {:<18} {:<15} {:<7} {:<5} {}"
    print(fmt.format("NAME", "BRIDGE", "ZONE", "CIDR", "GATEWAY", "NAT", "VMS", "ISOLATION"))
    for row in rows:
        print(fmt.format(*map(str, row)))
    if any(nets.takeable(v) for v in free):
        print("\nnot pvelab's yet: made in PVE; pvelab create --net NAME takes it over (VMs on it get isolation,\n"
              "ssh forwards and blocks)")
    if not all(nets.takeable(v) for v in free):
        print("\nits own: a vnet PVE gives no gateway (a VLAN, or no subnet): pvelab create --net NAME puts VMs on\n"
              "its own network as it is (its DHCP), with no rules of pvelab's")
    if free:
        print("net set / rename / remove change a vnet of PVE in PVE directly")


def parse(args, flags=()):
    """net create / set's words after the subcommand: (NAME, --cidr, --gateway, the flags given)."""
    name, cidr_opt, gw_opt, seen = None, None, None, set()
    it = iter(args[1:])
    for a in it:
        if a in flags:
            seen.add(a)
        elif (v := opt(a, it, "--cidr")) is not None:
            cidr_opt = v
        elif (v := opt(a, it, "--gateway")) is not None:
            gw_opt = v
        elif a.startswith("-") or name:
            bad_usage(USAGE)
        else:
            name = a
    return name, cidr_opt, gw_opt, seen


def cmd_net_create(args):
    name, cidr_opt, gw_opt, seen = parse(args, ("--no-isolate",))
    isolate = "--no-isolate" not in seen
    if not name or name in ("auto", "dhcp"):
        bad_usage("net create needs the network's name: pvelab net create NAME [--cidr CIDR] [--gateway ADDR]")
    with nets.cached_inputs():               # the hosts' routes and SDN subnets: read once for the range checks
        n = nets.resolve(name)
        if n is not None and is_plain(n):
            die(f"{name} is " + ("pvelab's DHCP bridge" if n == "dhcp" else "a bridge of this host already (not "
                                 "pvelab's)") + ": pick another name")
        if n is not None and not is_new_net(n):
            die(f"{nets.net_name(n)} exists already ({nets.cidr(n)}, gateway {nets.gateway(n)}): pvelab net list")
        require_quorum("no network can be made")
        n = pick_net(name, cidr_opt, gw_opt)
        other = foreign_use(n)
        if other:
            die(f"can't use {nets.net_name(n)}: {other}")
        if is_sdn(n):
            sdn.refuse_foreign()
        plan = Plan()
        plan.do(f"make {nets.net_name(n)}: {nets.cidr(n)}, gateway {nets.gateway(n)} (NAT to the internet), "
                f"{bridge(n)}" + (" on every host (SDN)" if is_sdn(n) else " on this host"))
        plan.do("owners kept apart (pvelab isolate --off lets them reach each other)" if isolate
                else "owners reach each other (--no-isolate)")
        plan.go()
        prepare_net(n, isolate, [this_node()])
    print(paint(f"\n{nets.net_name(n)} is ready; VMs get {nets.cidr(n)} addresses from "
                f"{next(nets.vm_addresses(n))}.", "green"))
    print(f"  pvelab create 20 --net {nets.net_name(n)}       20 VMs on it")


def cmd_net_set(args, dry=False):
    name, cidr_opt, gw_opt, _ = parse(args)
    if not name:
        bad_usage("net set needs the network's name: pvelab net set NAME [--cidr CIDR] [--gateway ADDR]")
    if vn := nets.free_vnet(name):
        return vnet_set(vn, cidr_opt, gw_opt, dry)
    net_set(name, cidr_opt, gw_opt, dry=dry)


def cmd_net_rename(args, dry=False):
    vms = "--rename-vms" in args
    args = [a for a in args if a != "--rename-vms"]
    if len(args) != 3 or any(a.startswith("-") for a in args[1:]):
        bad_usage("net rename takes the network's name and its new one: pvelab net rename OLD NEW [--rename-vms]")
    if vn := nets.free_vnet(args[1]):
        if vms:
            bad_usage(f"{args[1]} has no VMs of pvelab's: no --rename-vms")
        return vnet_rename(vn, args[2], dry)
    net_rename(args[1], args[2], dry=dry, rename_vms=vms)


def cmd_net_remove(args, dry=False):
    if len(args) != 2 or args[1].startswith("-"):
        bad_usage(USAGE)
    if vn := nets.free_vnet(args[1]):
        return vnet_remove(vn, dry)
    n = local_net(args[1], "remove")
    name, br, taken, old = nets.net_name(n), bridge(n), taken_over(n), nets.cidr(n)
    vms = guest_names(br)
    vms += [r["name"] for i, r in cred_rows().items() if r["net"] == n and r["name"] not in vms]   # recorded too
    if vms:
        die(f"{name} still has VMs: {ui.some(sorted(vms), 8)}. Destroy them first (pvelab destroy --net {name})")
    plan = Plan()
    plan.do(f"remove {name} ({old}, gateway {nets.gateway(n)}): pvelab's rules and "
            + (f"PVE vnet {br} with its subnet" if taken else f"its bridge {br}"))

    def work():
        remove_bridge(n, quiet=True)
        if taken:
            sdn.delete_vnet(br)
    if in_sdn(n):
        if not change_sdn(plan, dry, work):
            return
    elif plan.go(dry):
        work()
    else:
        return
    print(paint(f"{name} removed", "green"))


def guest_names(br):
    """The names of the guests with a card on bridge br (any node)."""
    return list(vm_names([i for i, _ in guests_on(br)]).values())


# --- a vnet made in PVE that pvelab has not taken over: changed in PVE's SDN config directly

def no_guests(vn, doing):
    """Stop when guests use PVE vnet vn: pvelab does not know their addresses."""
    vms = guest_names(vn["vnet"])
    if vms:
        die(f"{vn['vnet']} is used by VMs pvelab did not make ({ui.some(vms, 5)}): {doing} it in PVE "
            "(Datacenter -> SDN), not with pvelab")


def change_sdn(plan, dry, work):
    """Ask (the plan, with the reload warning), then work() under PVE's SDN lock and apply it."""
    plan.warn(sdn.RELOAD_WARNING)
    if not plan.go(dry):
        return False
    with sdn.session():
        work()
        sdn.apply()
    return True


def vnet_set(vn, cidr_opt, gw_opt, dry):
    if not cidr_opt and not gw_opt:
        die("nothing to change: give --cidr CIDR and/or --gateway ADDR")
    no_guests(vn, "change")
    v, old = vn["vnet"], vn["cidr"]
    if old is None and not cidr_opt:
        die(f"{v} has no subnet: give --cidr")
    new, gw = nets.next_range(old or nets.parse_cidr(cidr_opt), vn["gateway"], cidr_opt, gw_opt)
    if (new, gw) == (old, vn["gateway"]):
        die(f"{v} has {old} with the gateway {gw or '-'} already")
    if new != old and (why := nets.overlap(new, {k: o for k, o in nets.taken_ranges(None, new).items() if o != old})):
        die(why)                             # (its own subnet does not count)
    if why := nets.subnet_problem(v):
        die(why)
    plan = Plan()
    plan.do(f"PVE vnet {v}: {old or 'no subnet'} gateway {vn['gateway'] or '-'} -> {new} gateway {gw or '-'}"
            + (" (SNAT kept)" if vn["snat"] else ""))
    if change_sdn(plan, dry, lambda: sdn.replace_vnet_subnet(v, new, gw)):
        print(paint(f"{vn['alias'] or v} is {new} now, gateway {gw or '-'}.", "green"))


def vnet_rename(vn, new, dry):
    v = vn["vnet"]
    if why := nets.name_problem(new, None):
        die(f"{new}: {why}")
    if any(new in (r["vnet"], r["alias"]) for r in nets.pve_vnets()):
        die(f"a vnet is called {new} already")
    plan = Plan()
    plan.do(f"PVE vnet {v}: alias {vn['alias'] or '-'} -> {new}")
    if change_sdn(plan, dry, lambda: sdn.set_vnet_alias(v, new)):
        print(paint(f"{v} is called {new} now.", "green"))


def vnet_remove(vn, dry):
    no_guests(vn, "remove")
    v = vn["vnet"]
    plan = Plan()
    plan.do(f"delete PVE vnet {v} ({vn['alias'] or '-'}) and its subnet {vn['cidr'] or '-'}; zone {vn['zone']} stays")
    if change_sdn(plan, dry, lambda: sdn.delete_vnet(v)):
        print(paint(f"{vn['alias'] or v} removed", "green"))

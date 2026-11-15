"""pvelab net [list] | net create NAME [--cidr CIDR] [--gateway ADDR] [--no-isolate] | net set NAME
[--cidr CIDR] [--gateway ADDR] | net rename OLD NEW | net remove NAME
A class's network on its own: made before its VMs, listed, changed, renamed, removed once it has none."""

from ..args import opt
from ..config import bridge, in_sdn, is_sdn, PVE_GATEWAY_ZONES, taken_over
from .. import nets, sdn, ui
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours
from ..inventory import read_conf
from ..isolation import is_isolated
from ..networks import foreign_use, guests_on, is_new_net, remove_bridge
from ..nodes import require_quorum, this_node
from ..readdress import local_net, net_rename, net_set
from ..ui import paint, Plan
from ..util import die
from .create import pick_net, prepare_net

USAGE = ("net takes: list, create NAME [--cidr CIDR] [--gateway ADDR] [--no-isolate], "
         "set NAME [--cidr CIDR] [--gateway ADDR] [--dry-run], rename OLD NEW [--dry-run], or remove NAME [--dry-run]")


def cmd_net(args):
    """net, net list: every class network."""
    if args and args[0] != "list" or len(args) > 1:
        bad_usage(USAGE)
    net_list()


def net_list():
    """Every network: pvelab's (its own and the PVE vnets it took over) and every other vnet in PVE's SDN
    config. VMs: the guests with a card on it, from their PVE configs."""
    rows = []
    for n, (name, net) in sorted(nets.all_nets().items(), key=lambda kv: int(kv[0])):
        br = bridge(n)
        zone = nets.sdn_vnets().get(br, (None, None))[0] if in_sdn(n) else None
        nat = ("PVE" if (nets.pve_subnet(br) or (0, 0, False))[2] else "-") if nets.pve_gateway(n) else "pvelab"
        apart = ("kept apart" if is_isolated(n) else "reach each other") if bridge_is_ours(n) else "-"
        rows.append((name, br, zone or "-", net, nets.gateway(n), nat, len(guests_on(br)), apart))
    for v in nets.free_vnets():
        nat = "PVE" if v["snat"] and v["type"] in PVE_GATEWAY_ZONES else "-"
        rows.append((v["alias"] or v["vnet"], v["vnet"], v["zone"] or "-", v["cidr"] or "-", v["gateway"] or "-",
                     nat, len(guests_on(v["vnet"])), "not pvelab's yet"))
    if not rows:
        print("No networks yet. Make one: pvelab net create NAME --cidr 10.5.0.0/24 --gateway .254")
        return
    fmt = "{:<16} {:<9} {:<9} {:<18} {:<15} {:<7} {:<5} {}"
    print(fmt.format("NAME", "BRIDGE", "ZONE", "CIDR", "GATEWAY", "NAT", "VMS", "STUDENTS"))
    for r in rows:
        print(fmt.format(*map(str, r)))
    if any(r[7] == "not pvelab's yet" for r in rows):
        print("\nnot pvelab's yet: made in PVE; pvelab create --net NAME takes it over (VMs on it get isolation,\n"
              "ssh forwards and blocks); net set / rename / remove change it in PVE directly")


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
        plan.do("students kept apart (pvelab isolate --off lets them reach each other)" if isolate
                else "students reach each other (--no-isolate)")
        plan.go()
        prepare_net(n, isolate, [this_node()])
    print(paint(f"\n{nets.net_name(n)} is ready; VMs get {nets.cidr(n)} addresses from "
                f"{next(nets.vm_addresses(n))}.", "green"))
    print(f"  pvelab create 20 --net {nets.net_name(n)}       20 VMs on it")


def free(name):
    """The PVE vnet (pve_vnets row) name means, when it is no pvelab net (its id or alias), else None."""
    return nets.free_vnet(name) if nets.resolve(name) is None else None


def cmd_net_set(args, dry=False):
    name, cidr_opt, gw_opt, _ = parse(args)
    if not name:
        bad_usage("net set needs the network's name: pvelab net set NAME [--cidr CIDR] [--gateway ADDR]")
    if vn := free(name):
        return vnet_set(vn, cidr_opt, gw_opt, dry)
    net_set(name, cidr_opt, gw_opt, dry=dry)


def cmd_net_rename(args, dry=False):
    if len(args) != 3 or any(a.startswith("-") for a in args[1:]):
        bad_usage("net rename takes the network's name and its new one: pvelab net rename OLD NEW")
    if vn := free(args[1]):
        return vnet_rename(vn, args[2], dry)
    net_rename(args[1], args[2], dry=dry)


def cmd_net_remove(args, dry=False):
    if len(args) != 2 or args[1].startswith("-"):
        bad_usage(USAGE)
    if vn := free(args[1]):
        return vnet_remove(vn, dry)
    n = local_net(args[1], "remove")
    name, br, taken = nets.net_name(n), bridge(n), taken_over(n)
    vms = [(read_conf(i) or {}).get("name") or str(i) for i, _ in guests_on(br)]
    vms += [r["name"] for i, r in cred_rows().items() if r["net"] == n and r["name"] not in vms]   # recorded too
    if vms:
        die(f"{name} still has VMs: {ui.some(sorted(vms), 8)}. Destroy them first (pvelab destroy --net {name})")
    plan = Plan()
    plan.do(f"remove {name} ({nets.cidr(n)}, gateway {nets.gateway(n)}): pvelab's rules and "
            + (f"PVE vnet {br} with its subnet" if taken else f"its bridge {br}"))
    if in_sdn(n):
        plan.warn(sdn.RELOAD_WARNING)
    if not plan.go(dry):
        return
    if in_sdn(n):
        with sdn.session():
            remove_bridge(n, quiet=True)
            if taken:
                sdn.delete_vnet(br)
            sdn.apply(gone=[nets.cidr(n)] if taken else [])    # the vnet goes from every host
    else:
        remove_bridge(n, quiet=True)
    print(paint(f"{name} removed", "green"))


# --- a vnet made in PVE that pvelab has not taken over: changed in PVE's SDN config directly

def no_guests(vn, doing):
    """Stop when guests use PVE vnet vn: pvelab does not know their addresses."""
    vms = [(read_conf(i) or {}).get("name") or str(i) for i, _ in guests_on(vn["vnet"])]
    if vms:
        die(f"{vn['vnet']} is used by VMs pvelab did not make ({ui.some(vms, 5)}): {doing} it in PVE "
            "(Datacenter -> SDN), not with pvelab")


def change_sdn(plan, dry, work, gone=()):
    """Ask (the plan, with the reload warning), then work() under PVE's SDN lock and apply it (gone: the
    ranges whose SNAT rules go, see sdn.tidy_snat)."""
    plan.warn(sdn.RELOAD_WARNING)
    if not plan.go(dry):
        return False
    with sdn.session():
        work()
        sdn.apply(gone)
    return True


def vnet_set(vn, cidr_opt, gw_opt, dry):
    if not cidr_opt and not gw_opt:
        die("nothing to change: give --cidr CIDR and/or --gateway ADDR")
    no_guests(vn, "change")
    v, old = vn["vnet"], vn["cidr"]
    new = nets.parse_cidr(cidr_opt) if cidr_opt else old
    if new is None:
        die(f"{v} has no subnet: give --cidr")
    if new != old:
        with nets.cached_inputs():
            others = {k: o for k, o in nets.taken_ranges(None, new).items() if o != old}   # not its own
        if why := nets.overlap(new, others):
            die(why)
    gw = nets.parse_gateway(gw_opt, new) if gw_opt else (
        new.network_address + (int(vn["gateway"]) - int(old.network_address)) if vn["gateway"] and old else None)
    if gw is None or not nets.usable(gw, new):
        gw = new.network_address + 1
    plan = Plan()
    plan.do(f"PVE vnet {v}: {old or 'no subnet'} gateway {vn['gateway'] or '-'} -> {new} gateway {gw}"
            + (" (SNAT kept)" if vn["snat"] else ""))
    if change_sdn(plan, dry, lambda: sdn.replace_vnet_subnet(v, new, gw), gone=[old] if old and old != new else []):
        print(paint(f"{vn['alias'] or v} is {new} now, gateway {gw}.", "green"))


def vnet_rename(vn, new, dry):
    v = vn["vnet"]
    if why := nets.name_problem(new, None):
        die(f"{new}: {why}")
    if new in {r["alias"] for r in nets.pve_vnets()} | {r["vnet"] for r in nets.pve_vnets()}:
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
    if change_sdn(plan, dry, lambda: sdn.delete_vnet(v), gone=[vn["cidr"]] if vn["cidr"] else []):
        print(paint(f"{vn['alias'] or v} removed", "green"))

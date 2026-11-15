"""pvelab net [list] | net create NAME [--cidr CIDR] [--gateway ADDR] [--no-isolate] | net remove NAME
A class's network on its own: made before its VMs, listed, removed once it has none."""

import glob

from ..args import opt
from ..config import bridge, is_sdn, NODES_DIR
from .. import nets, sdn
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours
from ..isolation import is_isolated
from ..networks import foreign_use, is_new_net, remove_bridge
from ..nodes import require_quorum, this_node
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
    rows = cred_rows()
    found = nets.all_nets()
    if not found:
        print("No networks yet. Make one: pvelab net create NAME --cidr 10.5.0.0/24 --gateway .254")
        return
    fmt = "{:<16} {:<18} {:<15} {:<8} {:<5} {}"
    print(fmt.format("NAME", "CIDR", "GATEWAY", "BRIDGE", "VMS", "STUDENTS"))
    for n, (name, net) in sorted(found.items(), key=lambda kv: int(kv[0])):
        count = sum(1 for r in rows.values() if r["net"] == n)
        apart = "kept apart" if bridge_is_ours(n) and is_isolated(n) else "reach each other" if bridge_is_ours(n) else "-"
        print(fmt.format(name, str(net), str(nets.gateway(n)), bridge(n), count, apart))


def cmd_net_create(args):
    name, cidr_opt, gw_opt, isolate = None, None, None, True
    it = iter(args[1:])
    for a in it:
        if a == "--no-isolate":
            isolate = False
        elif (v := opt(a, it, "--cidr")) is not None:
            cidr_opt = v
        elif (v := opt(a, it, "--gateway")) is not None:
            gw_opt = v
        elif a.startswith("-") or name:
            bad_usage(USAGE)
        else:
            name = a
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
        plan.ask()
        prepare_net(n, isolate, [this_node()])
    print(paint(f"\n{nets.net_name(n)} is ready; VMs get {nets.cidr(n)} addresses from "
                f"{next(nets.vm_addresses(n))}.", "green"))
    print(f"  pvelab create 20 --net {nets.net_name(n)}       20 VMs on it")


def cmd_net_set(args, dry=False):
    from ..readdress import net_set
    name, cidr_opt, gw_opt = None, None, None
    it = iter(args[1:])
    for a in it:
        if (v := opt(a, it, "--cidr")) is not None:
            cidr_opt = v
        elif (v := opt(a, it, "--gateway")) is not None:
            gw_opt = v
        elif a.startswith("-") or name:
            bad_usage(USAGE)
        else:
            name = a
    if not name:
        bad_usage("net set needs the network's name: pvelab net set NAME [--cidr CIDR] [--gateway ADDR]")
    net_set(name, cidr_opt, gw_opt, dry=dry)


def cmd_net_rename(args, dry=False):
    from ..readdress import net_rename
    if len(args) != 3 or any(a.startswith("-") for a in args[1:]):
        bad_usage("net rename takes the network's name and its new one: pvelab net rename OLD NEW")
    net_rename(args[1], args[2], dry=dry)


def vm_confs_on(br):
    """Names of the VMs whose network card is on bridge br, on any host."""
    out = []
    for f in glob.glob(f"{NODES_DIR}/*/qemu-server/*.conf"):
        text = open(f).read().split("\n[", 1)[0]          # the current config, not its snapshots
        if f"bridge={br}," in text or f"bridge={br}\n" in text:
            name = next((l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("name: ")), f[-9:-5])
            out.append(name)
    return sorted(out)


def cmd_net_remove(args, dry=False):
    if len(args) != 2 or args[1].startswith("-"):
        bad_usage(USAGE)
    n = nets.resolve(args[1])
    if n is None:
        die(f"no network '{args[1]}' (see: pvelab net list)")
    name, br = nets.net_name(n), bridge(n)
    if not bridge_is_ours(n):
        die(f"{name}'s gateway is not on this host: run pvelab net remove {name} on the host that has {br}")
    vms = vm_confs_on(br)
    if vms:
        die(f"{name} still has VMs: {', '.join(vms[:8])}{' ...' if len(vms) > 8 else ''}. "
            f"Destroy them first (pvelab destroy --net {name})")
    plan = Plan()
    plan.do(f"remove {name} ({nets.cidr(n)}, gateway {nets.gateway(n)}) and its bridge {br}")
    if is_sdn(n):
        plan.warn("removing an SDN vnet reloads the network of every host: students on other SDN nets may "
                  "lose the connection for a few seconds")
    if dry:
        plan.show()
        print("dry run - nothing was changed")
        return
    plan.ask()
    if is_sdn(n):
        with sdn.session():
            remove_bridge(n, quiet=True)
            sdn.apply()                      # the vnet goes from every host
    else:
        remove_bridge(n, quiet=True)
    print(paint(f"{name} removed", "green"))

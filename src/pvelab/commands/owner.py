"""pvelab owner VM... KEY: whose VMs these are (owner=KEY in their descriptions; isolation follows).
pvelab owner --check: what the one-time recording of owners (migrate.py) would do, changing nothing."""

from collections import Counter

from .. import accounts, migrate, nets
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, on_net, read_conf, targets
from ..isolation import is_isolated, set_isolation
from ..nodes import node_of, on_node
from ..owners import is_shared_vm, net_name_of, owner_of, SHARED, with_tokens, without_net
from ..ui import paint
from ..util import die, LabError, parallel, run, warn


def refresh_isolation(on_nets):
    """Isolation tables follow owners: rebuilt for the nets that have it on."""
    for n in sorted(on_nets):
        if is_isolated(n):
            set_isolation(n)


def cmd_owner(args):
    if len(args) < 2:
        bad_usage("pvelab owner VM... KEY")
    *vms, arg = args
    if why := accounts.vm_name_problem(arg) or (arg == SHARED and "'shared' marks a net's shared VM"):
        bad_usage(f"KEY (an owner's name): {why}")
    confs = {i: read_conf(i) or die(f"VM {i}: no config") for i in targets(vms)}
    for c in confs.values():
        if is_shared_vm(c):
            n = desc_value(c, "net")
            die(f"{c.get('name')} is {nets.net_name(n)}'s shared VM: it is no one's (its fixed MAC and its rules "
                "stay with it)")

    keys = {i: without_net(arg, net_name_of(c)) for i, c in confs.items()}   # net2-alice01 on net2: alice01
    todo = {i: c for i, c in confs.items() if desc_value(c, "owner") != keys[i]}
    for i, c in confs.items():
        if i not in todo:
            print(f"{c.get('name')}: owner {keys[i]} already")

    def one(i):
        conf, key = todo[i], keys[i]
        desc = with_tokens(conf.get("description", ""), owner=key, student=None)
        try:
            run(*on_node(conf.get("_node") or node_of(i), "qm", "set", i, "--description", desc))
        except LabError as e:
            return False, f"{conf.get('name')}: not changed ({e})"
        was = owner_of(conf)
        return True, f"{conf.get('name')}: owner {key}" + (f" (was {was})" if was != key else "")
    done = parallel(one, sorted(todo))
    for _, line in done.values():
        print(line)
    changed = {i: desc_value(todo[i], "net") for i, (ok, _) in done.items() if ok}
    on_nets = set(changed.values()) - {"", "dhcp"}
    if on_nets:
        warn_layout(on_nets, {(n, keys[i]) for i, n in changed.items()}, inventory())
    refresh_isolation(on_nets)


def warn_layout(on_nets, moved, inv):
    """After owners changed on on_nets ((net, owner) pairs in moved): what pvelab expects of them - an
    owner's VMs of a net on one host, every owner with as many VMs (create's NxM)."""
    for n in sorted(on_nets):
        mine = [c for c in on_net(n, inv) if not is_shared_vm(c)]
        for key in sorted(k for m, k in moved if m == n):
            if len(hosts := {c.get("_node") for c in mine if owner_of(c) == key}) > 1:
                warn(f"{key}'s VMs on {nets.net_name(n)} are on {len(hosts)} hosts ({', '.join(sorted(hosts))}): "
                     f"pvelab move them together (pvelab move VM... --to HOST)")
        counts = Counter(owner_of(c) for c in mine).values()
        if len(set(counts)) > 1:
            warn(f"{nets.net_name(n)}: owners have {min(counts)} to {max(counts)} VMs - pvelab create on it "
                 "counts VMs per owner (NxM): even them out first")


def cmd_owner_check(args):
    if args[1:]:                             # args[0]: --check itself
        bad_usage("pvelab owner --check (no arguments)")
    inv = inventory()
    todo, problems = migrate.plan(inv)
    if not todo:
        print("every VM has its owner and tags recorded")
    for vmid, (desc, tags) in todo.items():
        conf = {**inv[vmid], "description": desc}
        who = "shared VM" if is_shared_vm(conf) else \
            f"owner {owner_of(conf)}" + (" (name alone)" if conf.get("name") in problems else "")
        print(f"{inv[vmid].get('name', vmid):<24} {who:<20} tags {tags}")
    if problems:
        print(paint("owner from the name alone (not NET-...): " + ", ".join(problems) + " - if another: "
                    f"pvelab owner {problems[0]} KEY", "yellow"))
    if todo:
        print(paint("\nrecorded by the next command that changes something (description and tags, no reboot)",
                    "dim"))

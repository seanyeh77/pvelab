"""pvelab owner VM... KEY: whose VMs these are (owner=KEY in their descriptions; isolation follows).
pvelab owner --check: what the one-time recording of owners (migrate.py) would do, changing nothing."""

from .. import accounts, migrate, nets
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, on_net, read_conf, targets, vm_names
from ..ifaces import gateway_elsewhere
from ..isolation import is_isolated, set_isolation
from ..nodes import node_of, on_node
from ..owners import by_owner, is_shared_vm, key_problem, net_name_of, owner_from_name, owner_of, SLOT, with_tokens, without_net
from ..ui import paint
from ..util import die, LabError, parallel, run, warn


def refresh_isolation(on_nets, inv, changed):
    """Isolation tables follow owners: rebuilt for the nets that have it on (inv: inventory()). An SDN net
    whose gateway is on another host has its isolation there: said (for the nets in changed, whose owners
    changed), not done from here."""
    for n in sorted(on_nets):
        name = nets.net_name(n)
        if is_isolated(n):
            try:
                set_isolation(n, confs=inv, tell_offline=True)     # an offline host keeps the old owners
            except (LabError, OSError) as e:     # the other nets still; this one: once the problem is fixed
                warn(f"{name}: isolation not rebuilt everywhere ({e}) - pvelab isolate --net {name}")
        elif n in changed and (where := gateway_elsewhere(n)):
            warn(f"{name}'s gateway is on {where}: if {name} is isolated, run pvelab isolate --net {name} there, "
                 "so its isolation follows the new owners")


def cmd_owner(args):
    if len(args) < 2:
        bad_usage("pvelab owner VM... KEY")
    *vms, arg = args
    if why := accounts.vm_name_problem(arg):
        bad_usage(f"KEY (an owner's name): {why}")
    confs = {i: read_conf(i) or die(f"VM {i}: no config") for i in targets(vms)}
    net = {i: desc_value(c, "net") for i, c in confs.items()}
    found = nets.all_nets()                  # read once: which nets this host knows, and their names
    known, called = nets.known_nets(found), vm_names(confs, confs)
    if far := [called[i] for i in confs if not nets.known_here(net[i], known)]:
        die(f"{', '.join(far)}: its net is not known on this host - run pvelab owner on the host with that net")
    keys = {i: without_net(arg, net_name_of(c)) for i, c in confs.items()}   # net2-alice01 on net2: alice01
    names = nets.pvelab_names(found)
    for k in set(keys.values()):
        if why := key_problem(k, names):
            bad_usage(f"KEY {arg}: {why}")
    for i, c in confs.items():
        if is_shared_vm(c):
            die(f"{called[i]} is {nets.net_name(net[i])}'s shared VM: it is no one's (its fixed MAC and its rules "
                "stay with it)")

    todo = {i: c for i, c in confs.items() if desc_value(c, "owner") != keys[i]}
    for i, c in confs.items():
        if i not in todo:
            print(f"{called[i]}: owner {keys[i]} already")

    def one(i):
        conf, key = todo[i], keys[i]
        desc = with_tokens(conf.get("description", ""), owner=key, student=None)
        try:
            run(*on_node(conf.get("_node") or node_of(i), "qm", "set", i, "--description", desc))
        except LabError as e:
            return False, f"{called[i]}: not changed ({e})"
        was = owner_of(conf)
        return True, f"{called[i]}: owner {key}" + (f" (was {was})" if was != key else "")
    done = parallel(one, sorted(todo))
    for _, line in done.values():
        print(line)
    on_nets = {n for n in net.values() if n not in ("", "dhcp")}
    by_net = {}                              # net -> {vmid: (its config before, its new owner)}
    for i, (ok, _) in done.items():
        if ok and net[i] in on_nets:
            by_net.setdefault(net[i], {})[i] = (todo[i], keys[i])
    if on_nets:                              # the named VMs' nets, changed or not (a run again rebuilds too)
        inv = inventory()
        if by_net:
            warn_layout(by_net, inv)
        refresh_isolation(on_nets, inv, set(by_net))


def warn_layout(by_net, inv):
    """After owners changed ({net: {vmid: (config before, new owner)}}): what pvelab expects of an owner's
    VMs of a net - on one host, one login and password (handout, passwd), as many VMs as the others
    (create's NxM) - and names create would take for another owner's slot."""
    rows = cred_rows()
    for n, moved in sorted(by_net.items()):
        mine = on_net(n, inv)
        groups = by_owner(mine, mine)
        for key in sorted({k for _, k in moved.values()}):
            ids = groups.get(key, [])
            if len(hosts := {mine[i].get("_node") for i in ids}) > 1:
                warn(f"{key}'s VMs on {nets.net_name(n)} are on {len(hosts)} hosts ({', '.join(sorted(hosts))}): "
                     f"pvelab move them together (pvelab move VM... --to HOST)")
            if len({(rows[i]["user"], rows[i]["password"]) for i in ids if i in rows}) > 1:
                warn(f"{key}'s VMs on {nets.net_name(n)} have different logins or passwords (one login card shows "
                     f"one): pvelab passwd {mine[ids[0]].get('name')} gives them one password")
        for c, key in moved.values():        # create would make the slot's owner a VM by this name
            if (was := owner_from_name(c.get("name", ""), net_name_of(c))) != key and SLOT.fullmatch(was):
                warn(f"{c.get('name')} is named as {was}'s VM: pvelab create on {nets.net_name(n)} stops at it - "
                     f"pvelab rename {c.get('name')} NEW")
        counts = [len(ids) for ids in groups.values()]
        if len(set(counts)) > 1:
            warn(f"{nets.net_name(n)}: owners have {min(counts)} to {max(counts)} VMs - pvelab create on it "
                 "counts VMs per owner (NxM): even them out first")


def cmd_owner_check(args):
    if args[1:]:                             # args[0]: --check itself
        bad_usage("pvelab owner --check (no arguments)")
    inv = inventory()
    near, names = migrate.reachable(inv), vm_names(inv, inv)
    every, problems, far_ids = migrate.plan(inv)
    todo = {v: t for v, t in every.items() if v in near}      # as the next command records them
    offline = [names[v] for v in every if v not in near]
    far = [names[v] for v in far_ids]      # whether their host is up or not: only it records them
    problems = [v for v in problems if v in near]
    if not todo and not offline and not far:
        print("every VM has its owner and tags recorded")
    for vmid, (desc, tags) in todo.items():
        conf = {**inv[vmid], "description": desc}
        who = "shared VM" if is_shared_vm(conf) else \
            f"owner {owner_of(conf)}" + (" (name alone)" if vmid in problems else "")
        print(f"{names[vmid]:<24} {who:<20} tags {tags}")
    if problems:
        print(paint("owner from the name alone (not NET-...): " + ", ".join(names[v] for v in problems)
                    + f" - if another: pvelab owner {names[problems[0]]} KEY", "yellow"))
    if offline:
        print(paint(f"not recorded yet, host offline: {', '.join(offline)} - the next command records them once "
                    "the host is back", "yellow"))
    if far:
        print(paint(f"not recorded from here, its net not known on this host: {', '.join(far)} - a command on "
                    "the host with that net records them", "yellow"))
    if todo:
        print(paint("\nrecorded by the next command that changes something (description and tags, no reboot)",
                    "dim"))

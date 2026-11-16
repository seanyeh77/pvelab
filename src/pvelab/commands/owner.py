"""pvelab owner VM... KEY: whose VMs these are (owner=KEY in their descriptions; isolation follows).
pvelab owner --check: what the one-time recording of owners (migrate.py) would do, changing nothing."""

from .. import accounts, migrate, nets
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, on_net, read_conf, targets
from ..isolation import is_isolated, set_isolation
from ..nodes import node_of, on_node
from ..owners import by_owner, is_shared_vm, key_problem, net_name_of, owner_from_name, owner_of, SLOT, with_tokens, without_net
from ..ui import paint
from ..util import die, LabError, parallel, run, warn


def refresh_isolation(on_nets, inv):
    """Isolation tables follow owners: rebuilt for the nets that have it on (inv: inventory(), if read)."""
    for n in sorted(on_nets):
        if is_isolated(n):
            set_isolation(n, confs=inv)


def cmd_owner(args):
    if len(args) < 2:
        bad_usage("pvelab owner VM... KEY")
    *vms, arg = args
    if why := accounts.vm_name_problem(arg):
        bad_usage(f"KEY (an owner's name): {why}")
    confs = {i: read_conf(i) or die(f"VM {i}: no config") for i in targets(vms)}
    keys = {i: without_net(arg, net_name_of(c)) for i, c in confs.items()}   # net2-alice01 on net2: alice01
    names = nets.every_name()
    for k in set(keys.values()):
        if why := key_problem(k, names):
            bad_usage(f"KEY {arg}: {why}")
    for c in confs.values():
        if is_shared_vm(c):
            n = desc_value(c, "net")
            die(f"{c.get('name')} is {nets.net_name(n)}'s shared VM: it is no one's (its fixed MAC and its rules "
                "stay with it)")

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
    by_net = {}                              # net -> {vmid: (its config before, its new owner)}
    for i, (ok, _) in done.items():
        if ok and (n := desc_value(todo[i], "net")) not in ("", "dhcp"):
            by_net.setdefault(n, {})[i] = (todo[i], keys[i])
    if by_net:
        inv = inventory()
        warn_layout(by_net, inv)
        refresh_isolation(by_net, inv)


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

"""pvelab owner VM... KEY: whose VMs these are (owner=KEY in their descriptions; isolation follows).
pvelab owner --check: what the one-time recording of owners (migrate.py) would do, changing nothing."""

from .. import accounts, migrate, nets
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, read_conf, targets
from ..isolation import is_isolated, set_isolation
from ..nodes import node_of, on_node
from ..owners import is_shared_vm, owner_of, with_tokens
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
    *vms, key = args
    if why := accounts.vm_name_problem(key):
        bad_usage(f"KEY (an owner's name): {why}")
    confs = {i: read_conf(i) or die(f"VM {i}: no config") for i in targets(vms)}
    todo = {i: c for i, c in confs.items() if desc_value(c, "owner") != key}
    for c in confs.values():
        if c not in todo.values():
            print(f"{c.get('name')}: owner {key} already")

    def one(i):
        conf = todo[i]
        desc = with_tokens(conf.get("description", ""), owner=key, student=None, role=None)
        try:
            run(*on_node(conf.get("_node") or node_of(i), "qm", "set", i, "--description", desc))
        except LabError as e:
            return False, f"{conf.get('name')}: not changed ({e})"
        was = owner_of(conf)
        return True, f"{conf.get('name')}: owner {key}" + (f" (was {was})" if was != key else "")
    done = parallel(one, sorted(todo))
    for _, line in done.values():
        print(line)
    on_nets = {desc_value(todo[i], "net") for i, (ok, _) in done.items() if ok} - {"", "dhcp"}
    for n in sorted(on_nets):                # pvelab keeps an owner's VMs of a net on one host
        hosts = {c.get("_node") for c in inventory().values()
                 if desc_value(c, "net") == n and desc_value(c, "owner") == key}
        if len(hosts) > 1:
            warn(f"{key}'s VMs on {nets.net_name(n)} are on {len(hosts)} hosts ({', '.join(sorted(hosts))}): "
                 f"pvelab move them together (pvelab move VM... --to HOST)")
    refresh_isolation(on_nets)


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

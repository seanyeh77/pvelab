"""pvelab owner VM... KEY: whose VMs these are (owner=KEY in their descriptions; isolation follows).
pvelab owner --check: what the one-time recording of owners (migrate.py) would do, changing nothing."""

from .. import accounts, migrate
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, read_conf, targets
from ..isolation import is_isolated, set_isolation
from ..nodes import node_of, on_node
from ..owners import owner_of, with_tokens
from ..ui import paint
from ..util import die, run


def refresh_isolation(on_nets):
    """Isolation tables follow owners: rebuilt for the nets that have it on."""
    for n in sorted(on_nets):
        if n != "dhcp" and is_isolated(n):
            set_isolation(n)


def cmd_owner(args):
    if len(args) < 2:
        bad_usage("pvelab owner VM... KEY")
    *vms, key = args
    if not accounts.NAME.fullmatch(key) or len(key) > 63:
        bad_usage(f"KEY: lowercase letters, digits and - (an owner's name), not '{key}'")
    on_nets = set()
    for vmid in targets(vms):
        conf = read_conf(vmid) or die(f"VM {vmid}: no config")
        if desc_value(conf, "owner") == key:
            print(f"{conf.get('name')}: owner {key} already")
            continue
        desc = with_tokens(conf.get("description", ""), owner=key, student=None, role=None)
        run(*on_node(conf.get("_node") or node_of(vmid), "qm", "set", vmid, "--description", desc))
        on_nets.add(desc_value(conf, "net") or "dhcp")
        print(f"{conf.get('name')}: owner {key}" + (f" (was {owner_of(conf)})" if owner_of(conf) != key else ""))
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
        who = "shared VM" if desc_value(conf, "role") == "shared" else \
            f"owner {owner_of(conf)}" + (" (name alone)" if conf.get("name") in problems else "")
        print(f"{inv[vmid].get('name', vmid):<24} {who:<20} tags {tags}")
    if problems:
        print(paint("owner from the name alone (not NET-...): " + ", ".join(problems) + " - if another: "
                    f"pvelab owner {problems[0]} KEY", "yellow"))
    if todo:
        print(paint("\nrecorded by the next command that changes something (description and tags, no reboot)",
                    "dim"))

"""VMs made before pvelab kept owners in PVE: their owner (owner=KEY) or role=shared recorded in the
description, and the tags pvelab;net-NAME, once, the first time a command that changes things runs.
qm set --description/--tags only: no reboot, no cloud-init run; a rollback keeps the description.

A VM whose name is not NET-... (a name from --accounts, or renamed by hand: web01 on net2) gets the owner
its name tells (web01; alice01-2: alice01), as pvelab grouped it before; it is listed once, when recorded,
with the fix if that is wrong: pvelab owner VM KEY."""

from . import nets
from .inventory import desc_value, inventory, is_managed
from .lifecycle import new_tags
from .nodes import offline_nodes, on_node
from .owners import is_recorded, is_shared_vm, legacy_student, owner_of, SHARED, with_tokens
from .ui import paint, some
from .util import LabError, parallel, run, warn


def owner_by_name(conf, net):
    """(tokens, ok) for a VM without owner= or role=: what pvelab reads for it now (owners.py), written down.
    ok False: a name that is not NET-... (the owner is that name alone)."""
    if is_shared_vm(conf, net):
        return {"role": SHARED}, True
    told = net == "dhcp" or legacy_student(conf) or conf.get("name", "").startswith(f"{nets.net_name(net)}-")
    return {"owner": owner_of(conf), "student": None}, bool(told)


def plan(inv):
    """({vmid: (description, tags)} to set, [names of VMs whose owner comes from a chosen name alone])."""
    todo, problems = {}, []
    for vmid, conf in sorted(inv.items()):
        if not is_managed(conf):
            continue
        net, desc, tags = desc_value(conf, "net") or "dhcp", conf.get("description", ""), conf.get("tags", "")
        if not is_recorded(conf):
            tokens, ok = owner_by_name(conf, net)
            if not ok:
                problems.append(conf.get("name") or str(vmid))
            desc = with_tokens(desc, **tokens)
        tags = new_tags(tags, net)
        if (desc, tags) != (conf.get("description", ""), conf.get("tags", "")):
            todo[vmid] = (desc, tags)
    return todo, problems


def ensure(dry):
    """Record owners and tags of VMs made before (plan): silent when there is nothing to do."""
    inv = inventory()
    offline = set(offline_nodes())           # their VMs once the host is back, without waiting now
    todo, problems = plan({v: c for v, c in inv.items() if c.get("_node") not in offline})
    if not todo:
        return
    told = sum(1 for v, (d, _) in todo.items() if d != inv[v].get("description", ""))
    print(paint(f"pvelab: recording owners in {told} VM(s) and tags in {len(todo)} (description and tags, "
                "no reboot)" + (" - dry run: not done" if dry else ""), "dim"))
    if problems:                             # said once: recorded now, these are not listed again
        print(paint(f"  owner from the name alone: {some(problems, 6)} - another owner: pvelab owner VM KEY",
                    "dim"))
    if dry:
        return

    def one(vmid):
        desc, tags = todo[vmid]
        try:
            run(*on_node(inv[vmid]["_node"], "qm", "set", vmid, "--description", desc, "--tags", tags))
        except LabError as e:
            warn(f"{inv[vmid].get('name')}: owner and tags not recorded ({e}); the next command tries again")
    parallel(one, todo)


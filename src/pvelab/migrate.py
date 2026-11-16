"""VMs made before pvelab kept owners in PVE: their owner (owner=KEY) or role=shared recorded in the
description, and the tags pvelab;net-NAME, once, the first time a command that changes things runs.
qm set --description/--tags only: no reboot, no cloud-init run; a rollback keeps the description.

A VM whose name is not NET-... (a name from --accounts, or renamed by hand: web01 on net2) gets the owner
its name tells (web01 is web01's; net2-x-2 is x's), as pvelab grouped it before (an older pvelab wrote
student= for the VMs of one account, alice01-1 and -2: those go by that); it is listed once, when recorded,
with the fix if that is wrong: pvelab owner VM KEY."""

from . import nets
from .config import TAG
from .inventory import desc_value, inventory, is_managed, tag_list, vm_names
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


def done(conf):
    """Whether a VM's owner and tags are recorded: owner= or role=, pvelab's tag and a net-* tag (which net's
    name the tag should have only the net's own host can tell)."""
    tags = tag_list(conf.get("tags"))
    return is_recorded(conf) and TAG in tags and any(t.startswith("net-") for t in tags)


def plan(inv):
    """({vmid: (description, tags)} to set, [VMIDs whose owner comes from a chosen name alone], [VMIDs left
    for the host their net is known on])."""
    todo, problems, far, known = {}, [], [], nets.known_nets()
    for vmid, conf in sorted(inv.items()):
        if not is_managed(conf):
            continue
        net, desc, tags = desc_value(conf, "net") or "dhcp", conf.get("description", ""), conf.get("tags", "")
        if not nets.known_here(net, known):
            if not done(conf):
                far.append(vmid)             # its name is known on its own host: recorded there
            continue
        if not is_recorded(conf):
            tokens, ok = owner_by_name(conf, net)
            if not ok:
                problems.append(vmid)
            desc = with_tokens(desc, **tokens)
        tags = new_tags(tags, net)
        if (desc, tags) != (conf.get("description", ""), conf.get("tags", "")):
            todo[vmid] = (desc, tags)
    return todo, problems, far


def ensure(dry):
    """Record owners and tags of VMs made before (plan): silent when there is nothing to do."""
    inv = inventory()
    todo, problems, _ = plan(reachable(inv))
    if not todo:
        return
    told = sum(1 for v, (d, _) in todo.items() if d != inv[v].get("description", ""))
    names = vm_names(todo, inv)
    print(paint(f"pvelab: recording owners in {told} VM(s) and tags in {len(todo)} (description and tags, "
                "no reboot)" + (" - dry run: not done" if dry else ""), "dim"))
    if problems:                             # said once: recorded now, these are not listed again
        print(paint(f"  owner from the name alone: {some([names[v] for v in problems], 6)} - another owner: "
                    "pvelab owner VM KEY", "dim"))
    if dry:
        return

    def one(vmid):
        desc, tags = todo[vmid]
        try:
            run(*on_node(inv[vmid]["_node"], "qm", "set", vmid, "--description", desc, "--tags", tags))
        except LabError as e:
            return str(e)
    if failed := {v: e for v, e in parallel(one, todo).items() if e}:
        warn(f"owner and tags not recorded in {len(failed)} VM(s) ({some([names[v] for v in failed], 5)}): "
             f"{next(iter(failed.values()))} - the next command tries again")


def reachable(inv):
    """inv without the VMs on offline hosts (recorded once the host is back, without waiting now)."""
    offline = set(offline_nodes())
    return {v: c for v, c in inv.items() if c.get("_node") not in offline}

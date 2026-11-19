"""pvelab migrate [--net NAME] [--dry-run]: VMs made before the registry kept pvelab's facts in their configs
(the description's managed-by line, the tags pvelab;net-NAME), credentials.tsv and blocks.json. migrate
writes each one's record (legacy_record) first, then takes pvelab's line and tags out of its config:
qm set --description/--tags/--delete only, no reboot; an admin's notes and own tags stay. Never automatic:
the other commands only say, once per run, how many VMs are left (hint).

A VM whose name is not NET-... (a name from --accounts, or renamed by hand: web01 on net2) gets the owner
its name tells (web01 is web01's; net2-x-2 is x's), as pvelab grouped it before (an older pvelab wrote
student= for the VMs of one account, alice01-1 and -2: those go by that); listed, with the fix if that is
wrong: pvelab owner VM KEY. Once no VM is left, credentials.tsv and blocks.json are renamed *.migrated-DATE."""

import os
import time

from . import blocks, credentials, nets, registry
from .args import net_arg, opt
from .config import LEGACY_TAGS, MARKER, TAG
from .credentials import cred_rows
from .helptext import bad_usage
from .inventory import inventory, is_managed, login_of, tag_list, vm_names, vm_value
from .legacy import legacy_record, strip_legacy
from .nodes import offline_nodes, on_node
from .owners import is_recorded, is_shared_vm, legacy_student
from .ui import paint, some
from .util import LabError, confirm_yes, parallel, run, warn


def marked(conf):
    """Whether a VM still keeps pvelab's facts in its config: made before the registry and not migrated,
    or recorded but with pvelab's line or tags back (a rollback to a snapshot from before)."""
    if not is_managed(conf):
        return False
    if "_rec" not in conf:
        return True
    return MARKER in (conf.get("description") or "") or bool({TAG, *LEGACY_TAGS} & set(tag_list(conf.get("tags"))))


def net_of(conf):
    return vm_value(conf, "net") or "dhcp"


def far(conf, known):
    """A VM without a record or owner=/role= whose net's name this host does not know: its owner (from its
    name, which starts with the net's name) is told on the host with that net."""
    return "_rec" not in conf and not is_recorded(conf) and not nets.known_here(net_of(conf), known)


def by_name(conf):
    """Whether the owner of a VM without owner=/role= comes from a name that is not NET-... alone."""
    if "_rec" in conf or is_recorded(conf):
        return False
    n = net_of(conf)
    if is_shared_vm(conf, n) or n == "dhcp" or legacy_student(conf):
        return False
    return not conf.get("name", "").startswith(f"{nets.net_name(n)}-")


def plan(inv, net=None):
    """[(vmid, record, new description, new tags)] for the VMs of inv that keep pvelab's facts in their
    config (marked); net: only that net's. A recorded VM's record is the one it has (not written again);
    VMs of a net not known here are left (far)."""
    known, rows, blocked, out = nets.known_nets(), None, None, []
    for vmid, conf in sorted(inv.items()):
        if not marked(conf) or (net is not None and net_of(conf) != net) or far(conf, known):
            continue
        if "_rec" in conf:
            rec = dict(conf["_rec"])
        else:
            if rows is None:
                rows, blocked = cred_rows(), blocks.blocks_load()
            rec = legacy_record(vmid, conf, rows.get(vmid), blocked)
        out.append((vmid, rec, *strip_legacy(conf.get("description", ""), conf.get("tags", ""))))
    return out


def line(vmid, conf, rec, desc, tags, name):
    """One VM's line: whose it is, its login, what its description and tags lose and keep."""
    who = "shared" if rec.get("role") == "shared" else f"owner {rec.get('owner') or '-'}"
    login = login_of(vmid, {"user": rec.get("user")}, conf)
    if desc == (conf.get("description") or ""):
        d = "description unchanged"
    else:
        d = "description: pvelab's line removed" + (", notes kept" if desc else "")
    old, new = tag_list(conf.get("tags")), tag_list(tags)
    gone = [t for t in old if t not in new]
    t = f"tags: {', '.join(gone)} removed" + (f", {', '.join(new)} kept" if new else "") if gone else "tags unchanged"
    return f"{name}: {who}, login {login}; {d}; {t}"


def qm_args(conf, desc, tags):
    """qm set's options that give a config desc and tags ("" deletes it); [] when it has them already."""
    out, delete = [], []
    for key, new in (("description", desc), ("tags", tags)):
        if new != (conf.get(key) or ""):
            if new:
                out += [f"--{key}", new]
            else:
                delete.append(key)
    return out + (["--delete", ",".join(delete)] if delete else [])


def cmd_migrate(args, dry=False):
    net, it = None, iter(args)
    for a in it:
        if (v := opt(a, it, "--net")) is not None:
            net = net_arg(v)
        else:
            bad_usage(f"pvelab migrate [--net NAME] [--dry-run] (not '{a}')")
    inv = inventory()
    offline = set(offline_nodes())
    near = {v: c for v, c in inv.items() if c.get("_node") not in offline}
    items = plan(near, net)
    names = vm_names(inv, inv)
    known = nets.known_nets()
    mine = [v for v, c in inv.items() if marked(c) and (net is None or net_of(c) == net)]
    elsewhere = [names[v] for v in mine if far(inv[v], known)]     # whether its host is up or not
    away = [names[v] for v in mine if v not in near and not far(inv[v], known)]

    if not items:
        print("no VM keeps pvelab's facts in its description or tags" + (f" on {nets.net_name(net)}" if net else ""))
    for vmid, rec, desc, tags in items:
        print(line(vmid, inv[vmid], rec, desc, tags, names[vmid]))
    if alone := [names[v] for v, *_ in items if by_name(inv[v])]:
        print(paint(f"owner from the name alone: {some(alone, 6)} - another owner: pvelab owner VM KEY", "dim"))
    if away:
        print(paint(f"left, host offline: {some(away, 6)} - pvelab migrate again once the host is back", "yellow"))
    if elsewhere:
        print(paint(f"left, its net not known on this host: {some(elsewhere, 6)} - pvelab migrate on the host "
                    "with that net", "yellow"))
    new = {v: rec for v, rec, *_ in items if "_rec" not in inv[v]}
    if items:
        print(f"{len(items)} VM(s): {len(new)} record(s) written to {registry.REGISTRY}, then pvelab's line and "
              "tags taken out of their configs (no reboot)" + (" - dry run: nothing changed" if dry else ""))
    if dry:
        return
    if items:
        confirm_yes(f"Plan: migrate {len(items)} VM(s) as listed above - no reboot; the VMs keep running.")
        if new:                                  # the record first: from now on it wins over the config
            registry.update(new, {v: inv[v] for v in new})

    todo = {v: (desc, tags) for v, _, desc, tags in items}

    def one(vmid):
        desc, tags = todo[vmid]
        if not (opts := qm_args(inv[vmid], desc, tags)):
            return None
        try:
            run(*on_node(inv[vmid]["_node"], "qm", "set", vmid, *opts))
        except LabError as e:
            return str(e)
    failed = {v: e for v, e in parallel(one, todo).items() if e}
    if failed:
        warn(f"pvelab's line and tags not taken out of {len(failed)} VM(s) ({some([names[v] for v in failed], 5)}): "
             f"{next(iter(failed.values()))} - their records are written; pvelab migrate again cleans them")
    done = set(todo) - set(failed)
    left = [v for v, c in inv.items() if marked(c) and v not in done]
    retire(left)


def retire(left):
    """credentials.tsv and blocks.json renamed *.migrated-DATE once no VM (left: those still keeping
    pvelab's facts in their config, in the whole cluster) reads them; else kept, saying why."""
    files = [f for f in (credentials.CRED, blocks.BLOCKS_FILE) if os.path.exists(f)]
    if not files:
        return
    if left:
        print(paint(f"{', '.join(os.path.basename(f) for f in files)} kept: {len(left)} VM(s) not migrated "
                    "yet read them", "dim"))
        return
    day = time.strftime("%Y-%m-%d")
    for f in files:
        to, n = f"{f}.migrated-{day}", 1
        while os.path.exists(to):
            to, n = f"{f}.migrated-{day}.{n}", n + 1
        os.replace(f, to)
        print(f"{os.path.basename(f)} renamed {to}: no VM reads it any more")


def hint():
    """One dim line when VMs still keep pvelab's facts in their configs (other commands, once per run);
    quiet when the VMs cannot be read now."""
    try:
        n = sum(1 for c in inventory().values() if marked(c))
    except (LabError, OSError):
        return
    if n:
        print(paint(f"pvelab: {n} VM(s) still keep pvelab's facts in their description/tags: pvelab migrate "
                    "--dry-run shows the change (no reboot)", "dim"))

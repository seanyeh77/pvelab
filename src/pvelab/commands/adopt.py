"""pvelab adopt VM --shared | --owner NAME: a VM made by hand (PVE's web UI) whose net0 is on one of pvelab's
nets becomes pvelab's - its record is written and the net's rules follow it. Nothing in the VM changes."""

from .. import accounts, nets, registry
from ..adopting import (adopt_net, check_agent, disks, has_vendor_data, login, nics, owner_key, pick_ip,
                        pvelab_bridges, shared_problem, strays)
from ..args import opt
from ..config import is_sdn, READY_TIMEOUT, SNAP, SNIPPET
from ..credentials import cred_rows
from ..expose import is_exposed, listening_ports, port_problem, ssh_port
from ..helptext import bad_usage
from ..inventory import has_cloudinit, inventory, is_managed, on_net, snapshot_names, vm_status
from ..isolation import is_isolated
from ..networks import ensure_gw_mac, refresh_net
from ..nodes import require_quorum, this_node
from ..owners import by_owner, group_logins, warn_logins
from ..registry import uuid_of
from ..ui import Plan, paint
from ..util import did_you_mean, die, LabError, warn
from .owner import warn_layout

USAGE = "pvelab adopt VM --shared | --owner NAME [--user USER] [--password PW] [--ip ADDR]"


def parse(args):
    """{"vm", "shared", "owner", "user", "password", "ip"} of adopt's arguments, or bad_usage."""
    out = {"vm": None, "shared": False, "owner": None, "user": None, "password": None, "ip": None}
    it = iter(args)
    for a in it:
        if a == "--shared":
            out["shared"] = True
        elif (v := opt(a, it, "--owner")) is not None:
            out["owner"] = v
        elif (v := opt(a, it, "--user")) is not None:
            out["user"] = v
        elif (v := opt(a, it, "--password")) is not None:
            out["password"] = v
        elif (v := opt(a, it, "--ip")) is not None:
            out["ip"] = v
        elif a.startswith("-") or out["vm"] is not None:
            bad_usage(USAGE)
        else:
            out["vm"] = a
    if out["vm"] is None:
        bad_usage(USAGE)
    if out["shared"] and out["owner"] is not None:
        bad_usage("--shared or --owner NAME: one of them")
    if not out["shared"] and out["owner"] is None:
        bad_usage("whose is it? --shared (the net's shared VM) or --owner NAME")
    if out["password"] is not None and (why := accounts.password_problem(out["password"])):
        bad_usage(f"--password: {why}")
    return out


def find_vm(arg, inv):
    """The VMID of VM arg (a VMID, or the one VM by that name) in inv (inventory()) whose config was read, or
    die."""
    if arg.isdigit() and int(arg) in inv:
        if inv[int(arg)] is None:            # listed, then its config could not be read
            die(f"no VM {arg}: its config is gone (destroyed or migrated meanwhile?) - run pvelab adopt again")
        return int(arg)
    named = [i for i, c in inv.items() if (c or {}).get("name") == arg]
    if not named:
        die(f"no VM called '{arg}'" + did_you_mean(arg, [s.name for s in strays(inv)]))
    if len(named) > 1:
        die(f"several VMs are called {arg} ({', '.join(map(str, named))}): give its VMID")
    return named[0]


def refuse(vmid, conf, inv):
    """Net n of VM vmid, once nothing stops pvelab adopting it (else die, saying how to fix it)."""
    n, why = adopt_net(conf, pvelab_bridges())
    if why:
        die(why)
    name = conf.get("name") or ""
    if not uuid_of(conf):
        die(f"{name or 'VM'} ({vmid}) has no smbios1 uuid, which pvelab's record is matched by: qm set {vmid} "
            "--smbios1 uuid=$(cat /proc/sys/kernel/random/uuid), then run pvelab adopt again")
    if not name:
        die(f"VM {vmid} has no name: qm set {vmid} --name NAME, then run pvelab adopt again")
    if why := accounts.vm_name_problem(name):
        die(f"{name}: {why} - qm set {vmid} --name NEW, then run pvelab adopt again")
    if other := next((i for i, c in inv.items() if i != vmid and is_managed(c) and c.get("name") == name), None):
        die(f"VM name {name} is pvelab's VM {other} already: give one of them another name first (pvelab rename, "
            f"or qm set {vmid} --name NEW)")
    return n


def adopt_problem(conf, inv):
    """(n, why): net n of VM conf, and why pvelab adopt refuses it whatever it is given (refuse; a cloud-init drive
    without ciuser: login) - n None then - or None when nothing does; the wizard asks its questions only then."""
    try:
        n = refuse(conf["_vmid"], conf, inv)
        if has_cloudinit(conf):
            login(conf, None)
    except LabError as e:
        return None, str(e)
    return n, None


def cmd_adopt(args, dry=False):
    o = parse(args)
    require_quorum("no VM can be adopted")
    inv = inventory()
    vmid = find_vm(o["vm"], inv)
    conf = inv[vmid]
    n = refuse(vmid, conf, inv)
    name, net, node = conf["name"], nets.net_name(n), conf.get("_node")
    if o["shared"]:
        if why := shared_problem(conf, n, inv):
            die(why)
        key = None
    else:
        key = owner_key(o["owner"], n, conf)
    user = login(conf, o["user"])
    rows = cred_rows()
    ip, source = pick_ip(conf, n, o["ip"], rows, inv)
    note = check_agent(conf, n, ip, source, lambda: vm_status(vmid))
    exposed, isolated = is_exposed(n), is_isolated(n)
    port = ssh_port(vmid) if exposed else None
    closed = port_problem(vmid, listening_ports()) if exposed else None     # its ssh port can't be forwarded
    record = {"name": name, "net": n, "ip": ip, "user": user, "password": o["password"] or None, "owner": key,
              "role": "shared" if key is None else None}

    plan = Plan()
    theirs = []
    if key is None:
        plan.do(f"record {name} ({vmid}, on {node}) as pvelab's: {net}'s shared VM, which every owner of {net} reaches")
    else:
        mine = on_net(n, inv)
        theirs = by_owner(mine, mine).get(key, [])
        plan.do(f"record {name} ({vmid}, on {node}) as pvelab's: owner {key}'s VM"
                + (f" (with their {len(theirs)} VM(s) on {net})" if theirs else " (a new owner)"))
    said = {"ipconfig0": "its cloud-init ipconfig0", "config": "its ipconfig0, which no cloud-init drive applies",
            "--ip": "as given"}
    plan.do(f"address {ip} ({said[source]}), login {user}, "
            "password " + ("as given" if o["password"] else f"none recorded (pvelab passwd {name} sets one)"))
    rules = (["isolation"] if isolated else []) + ["blocks"] + ([f"ssh port {port} from outside"] if exposed and not closed else [])
    plan.do(f"{net}'s rules follow it: {', '.join(rules)}")
    plan.do("nothing in the VM changes (no reboot); from now on pvelab's commands act on it as on the VMs it made - "
            "pvelab destroy too")
    if theirs:                               # one login card for the owner's VMs on the net
        rows_with = {**{i: rows[i] for i in theirs if i in rows},
                     vmid: {"name": name, "user": user, "password": o["password"] or ""}}
        login_with = group_logins(rows_with, rows_with)
        warn_logins(key, net, login_with, rows_with, name, card="their login card", users=False, out=plan.warn)
        if others := sorted(login_with.users - {user}):
            plan.warn(f"{name} logs in as {user}, {key}'s other VMs on {net} as {', '.join(others)}: their login card "
                      f"shows each VM's user - or adopt {name} under another --owner")
    would = {**conf, "_rec": record}
    warn_layout({n: {vmid: (would, key)}}, {**inv, vmid: would}, logins=False, out=plan.warn)   # logins: above
    if closed:
        plan.warn(f"{net} is exposed, but port {port} {closed}: {name}'s ssh is not reachable from outside")
    elif exposed:
        plan.warn(f"{net} is exposed: {name}'s ssh becomes reachable from outside on port {port}")
    if note:
        plan.warn(note)
    if not has_cloudinit(conf):
        plan.warn(f"{name} has no cloud-init drive: pvelab rename and net set change its config only (its "
                  "hostname and address are changed in the VM by hand), yet while it runs they reboot it and wait "
                  f"for cloud-init up to {READY_TIMEOUT // 60} min - stop it first; pvelab passwd sets a password "
                  "only while it runs")
    elif not has_vendor_data(conf):
        plan.warn(f"{name} has no pvelab vendor-data (cicustom vendor=local:snippets/{SNIPPET}): cloud-init in it "
                  "makes new SSH host keys (its users get a host key warning) when pvelab rename or net set reboots "
                  "it while it runs, and at its next boot or rollback after pvelab passwd (a new cloud-init password "
                  "in its config and snapshots)")
    if (found := disks(conf)) and "scsi0" not in found:
        plan.warn(f"{name}'s disk is {', '.join(found)}, not scsi0: pvelab resize --disk and pvelab list's size "
                  f"column go by scsi0 (list shows 0G) - qm disk resize {vmid} {found[0]} SIZE resizes it")
    if SNAP not in (snaps := snapshot_names(vmid)):
        plan.warn(f"{name} has no '{SNAP}' snapshot, which pvelab rollback goes back to by default"
                  + (f" (its snapshots: {', '.join(snaps)})" if snaps else "")
                  + f": pvelab snapshot {name} takes it now, or pvelab rollback {name} --to NAME names one")
    for nic, br, _ in nics(conf):
        if nic != "net0" and br:
            plan.warn(f"{name} also has {nic} on {br}: pvelab's rules cover net0 only")
    if not plan.go(dry):
        return

    registry.update({vmid: record}, {vmid: conf})
    try:
        if is_sdn(n) and node != this_node():    # its first VM on another host: the gateway's fixed MAC
            ensure_gw_mac(n)
        refresh_net(n)                           # isolation, blocks, ssh forwards: as create does
    except (LabError, OSError) as e:             # the parts that name it: what makes them again
        again = ([f"pvelab expose --net {net}"] if exposed else []) \
            + ([f"pvelab isolate --net {net}"] if isolated else [])
        warn(f"{name} is recorded; {net}'s rules not brought up to date ({e})"
             + (f" - once that is fixed: {'; '.join(again)}" if again else ""))
    print(paint(f"\n{name} is pvelab's now.", "green"))
    print(f"  pvelab list --net {net}")

"""pvelab adopt VM --shared | --owner NAME: a VM made by hand (PVE's web UI) whose net0 is on one of pvelab's
nets becomes pvelab's - its record is written and the net's rules follow it. Nothing in the VM changes."""

from .. import accounts, nets, registry
from ..adopting import (check_agent, disks, firewall_problem, has_cloudinit, has_vendor_data, host_problem, login, nics, owner_key, pick_ip,
                        pvelab_bridges, shared_problem, strays)
from ..args import opt
from ..config import is_sdn, READY_TIMEOUT, SNAP, SNIPPET
from ..credentials import cred_rows
from ..expose import is_exposed, ssh_port
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours, gateway_elsewhere
from ..inventory import inventory, is_managed, net0, on_net, read_conf, snapshot_names, vm_status
from ..isolation import is_isolated
from ..networks import ensure_gw_mac, refresh_net
from ..nodes import require_quorum, this_node
from ..owners import by_owner
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
    if out["password"] is not None and not accounts.password_ok(out["password"]):
        bad_usage("--password: a password, without tabs or line breaks")
    return out


def find_vm(arg, inv):
    """The VMID of VM arg (a VMID, or the one VM by that name) in inv (inventory()), or die."""
    if arg.isdigit() and int(arg) in inv:
        return int(arg)
    named = [i for i, c in inv.items() if (c or {}).get("name") == arg]
    if not named:
        die(f"no VM called '{arg}'" + did_you_mean(arg, [s.name for s in strays(inv)]))
    if len(named) > 1:
        die(f"several VMs are called {arg} ({', '.join(map(str, named))}): give its VMID")
    return named[0]


def refuse(vmid, conf, inv):
    """Net n of VM vmid, once nothing stops pvelab adopting it (else die, saying how to fix it)."""
    name = conf.get("name") or ""
    called = f"{name or 'VM'} ({vmid})"
    if conf.get("template") == "1":
        die(f"{called} is a template: pvelab adopt takes VMs")
    if "_rec" in conf:
        die(f"{called} is pvelab's already (pvelab list shows it)")
    if is_managed(conf):
        die(f"{called} was made by an older pvelab: pvelab migrate records it")
    if not uuid_of(conf):
        die(f"{called} has no smbios1 uuid, which pvelab's record is matched by: qm set {vmid} --smbios1 "
            "uuid=$(cat /proc/sys/kernel/random/uuid), then run pvelab adopt again")
    if not name:
        die(f"VM {vmid} has no name: qm set {vmid} --name NAME, then run pvelab adopt again")
    if why := accounts.vm_name_problem(name):
        die(f"{name}: {why} - qm set {vmid} --name NEW, then run pvelab adopt again")
    if other := next((i for i, c in inv.items() if i != vmid and is_managed(c) and c.get("name") == name), None):
        die(f"VM name {name} is pvelab's VM {other} already: give one of them another name first (pvelab rename, "
            f"or qm set {vmid} --name NEW)")
    found = nets.all_nets()
    bridges = pvelab_bridges(found)
    br = net0(conf)[0]
    if br not in bridges:
        names = nets.pvelab_names(found)
        die(f"{name}'s net0 is on {br or 'no bridge'}, not on one of pvelab's nets ({', '.join(names) or 'none yet'}): "
            "pvelab adopt takes a VM whose net0 is on its net")
    n = bridges[br]
    if not bridge_is_ours(n):
        die(f"{nets.net_name(n)}'s gateway is on {gateway_elsewhere(n) or 'another host'}: its rules (isolation, ssh "
            "forwards, blocks) are there - run pvelab adopt on that host")
    if why := host_problem(n, conf.get("_node"), name) or firewall_problem(conf):
        die(why)
    return n


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
    note = check_agent(conf, ip, source, lambda: vm_status(vmid))
    exposed, isolated = is_exposed(n), is_isolated(n)
    port = ssh_port(vmid) if exposed else None

    plan = Plan()
    theirs = []
    if key is None:
        plan.do(f"record {name} ({vmid}, on {node}) as pvelab's: {net}'s shared VM, which every owner of {net} reaches")
    else:
        mine = on_net(n, inv)
        theirs = by_owner(mine, mine).get(key, [])
        plan.do(f"record {name} ({vmid}, on {node}) as pvelab's: owner {key}'s VM"
                + (f" (with their {len(theirs)} VM(s) on {net})" if theirs else " (a new owner)"))
    plan.do(f"address {ip} ({'its cloud-init ipconfig0' if source == 'ipconfig0' else 'as given'}), login {user}, "
            "password " + ("as given" if o["password"] else f"none recorded (pvelab passwd {name} sets one)"))
    rules = (["isolation"] if isolated else []) + ["blocks"] + ([f"ssh port {port} from outside"] if exposed else [])
    plan.do(f"{net}'s rules follow it: {', '.join(rules)}")
    plan.do("nothing in the VM changes (no reboot); from now on pvelab's commands act on it as on the VMs it made - "
            "pvelab destroy too")
    if not o["password"] and any((rows.get(i) or {}).get("password") for i in theirs):
        plan.warn(f"{key}'s other VMs on {net} have a password recorded, {name} none: their login card shows none - "
                  f"pvelab passwd {name} gives all of them one password")
    if exposed:
        plan.warn(f"{net} is exposed: {name}'s ssh becomes reachable from outside on port {port}")
    if note:
        plan.warn(note)
    if not has_cloudinit(conf):
        plan.warn(f"{name} has no cloud-init drive: pvelab rename and net set change its config only (its "
                  "hostname and address are changed in the VM by hand), yet while it runs they reboot it and wait "
                  f"for cloud-init up to {READY_TIMEOUT // 60} min - stop it first; pvelab passwd sets a password "
                  "only while it runs")
    elif not has_vendor_data(conf):
        plan.warn(f"{name} has no pvelab vendor-data (cicustom vendor=local:snippets/{SNIPPET}): pvelab rename and "
                  "net set reboot it while it runs, and cloud-init in it makes new SSH host keys (its users get a "
                  "host key warning)")
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

    registry.update({vmid: {"name": name, "net": n, "ip": ip, "user": user, "password": o["password"] or None,
                            "owner": key, "role": "shared" if key is None else None}}, {vmid: conf})
    try:
        if is_sdn(n) and node != this_node():    # its first VM on another host: the gateway's fixed MAC
            ensure_gw_mac(n)
        refresh_net(n)                           # isolation, blocks, ssh forwards: as create does
    except (LabError, OSError) as e:             # the parts that name it: what makes them again
        again = ([f"pvelab expose --net {net}"] if exposed else []) \
            + ([f"pvelab isolate --net {net}"] if isolated else [])
        warn(f"{name} is recorded; {net}'s rules not brought up to date ({e})"
             + (f" - once that is fixed: {'; '.join(again)}" if again else ""))
    if key is not None:
        warn_layout({n: {vmid: (conf, key)}}, {**inv, vmid: read_conf(vmid)})     # its config with the record
    print(paint(f"\n{name} is pvelab's now.", "green"))
    print(f"  pvelab list --net {net}")

"""pvelab node remove NAME: take a host that is gone for good out of the cluster, from the host that stays.

PVE's way (pvecm chapter, "Remove a Cluster Node"): the host is powered off and never comes back as it is;
on a remaining host, pvecm delnode NAME (pvecm expected first when the hosts left have no quorum alone).
pvelab also removes what PVE leaves behind: /etc/pve/nodes/NAME (its guest configs: only pvelab's
template copies may be there), its ssh host keys in /etc/pve/priv/known_hosts, and its IP in the SDN zone's
VXLAN peers (an SDN apply: every host reloads its network). Everything is checked first, read-only; the
plan is asked once; config.db, corosync and the node directory are backed up before any change."""

import os
import re
import shutil
import time

from . import sdn, ui
from .config import COROSYNC_CONF, LAB_DIR, read_text
from .inventory import read_conf
from .nodes import members, quorate, this_node
from .renaming import BACKUP_SH, CONFIG_DB, delnode, guest_confs, node_block, node_dir
from .templates import source_of
from .ui import paint, Plan, Spinner
from .util import die, pipe, replace_file

KNOWN_HOSTS = "/etc/pve/priv/known_hosts"


def ring_addr(name):
    """name's ring0_addr in corosync.conf (its IP), or None."""
    m = re.search(r"ring0_addr:\s*(\S+)", node_block(read_text(COROSYNC_CONF), name) or "")
    return m.group(1) if m else None


def survey(name):
    """The job: the host, its address, its guests (which are template copies), the SDN peers."""
    zone = sdn.zone()
    job = {"name": name, "this": this_node(), "members": members(), "addr": None, "copies": {}, "others": [],
           "quorate": quorate(), "peers": (zone or {}).get("peers", ""),
           "backup": f"{LAB_DIR}/node-remove-backup-{time.strftime('%Y%m%d-%H%M%S')}"}
    if name not in job["members"]:
        return job
    job["addr"] = job["members"][name].get("ip") or ring_addr(name)
    for kind, v in guest_confs(name):
        conf = read_conf(v, f"{node_dir(name)}/{kind}/{v}.conf") or {}
        if kind == "qemu-server" and conf.get("template") == "1" and source_of(conf) is not None:
            job["copies"][v] = conf.get("name", "")
        else:
            job["others"].append(f"{v} ({conf.get('name') or kind})")
    return job


def problems(job):
    """What stops the removal (each with what to do); [] when it can go on."""
    name, mem = job["name"], job["members"]
    if name not in mem:
        return [f"{name} is not a host of this cluster (hosts: {', '.join(sorted(mem))})"]
    if name == job["this"]:
        return [f"{name} is this host: run pvelab node remove {name} on a host that stays"]
    out = []
    if mem[name]["online"]:
        out.append(f"{name} is online: power it off first (PVE removes only a host that is off for good)")
    if job["others"]:
        out.append(f"{name} still has VMs or containers: {', '.join(job['others'])}. Move them (pvelab move "
                   f"VM --to HOST, while it is online) or destroy them first; only pvelab's template copies may stay")
    if not any(m["online"] for n, m in mem.items() if n != name):
        out.append("no other host is online")
    return out


def peers_after(job):
    """The SDN zone's VXLAN peers without the removed host's address, or None: nothing to change."""
    peers = [p for p in job["peers"].split(",") if p]
    if not job["addr"] or job["addr"] not in peers:
        return None
    return ",".join(p for p in peers if p != job["addr"])


def show_plan(job, dry=False):
    name, left = job["name"], [n for n in sorted(job["members"]) if n != job["name"]]
    plan = Plan()
    plan.do(f"back up config.db, /etc/corosync, corosync.conf and /etc/pve/nodes/{name} to {job['backup']}/")
    if not job["quorate"]:
        plan.do(f"pvecm expected 1 first: without {name}, {' and '.join(left)} {'has' if len(left) == 1 else 'have'} "
                "no quorum")
    plan.do(f"remove {name} ({job['addr'] or 'no address known'}) from the cluster: pvecm delnode {name}")
    plan.do(f"remove /etc/pve/nodes/{name}" + (" with its template copies " + ", ".join(
        f"{n} ({v})" for v, n in job["copies"].items()) + " (their disks were on it)" if job["copies"] else ""))
    plan.do(f"remove {name}'s ssh host keys from {KNOWN_HOSTS}")
    after = peers_after(job)
    if after is not None:
        plan.do(f"SDN zone {sdn.SDN_ZONE}: VXLAN peers {after} (without {job['addr']}), applied on every host")
        plan.warn("applying SDN reloads the network of every host: students on SDN nets may lose the "
                  "connection for a few seconds - do it outside class time")
    plan.warn(f"{name} still has the cluster's configuration: never start it on this network as it is. "
              "Reinstall it before using it again (pvelab node add can join it then)")
    plan.show() if dry else plan.ask()


def back_up(job):
    os.makedirs(job["backup"], mode=0o700, exist_ok=True)
    with Spinner("backing up the cluster configuration") as line:
        script = BACKUP_SH.format(db=CONFIG_DB, dbrel=CONFIG_DB.lstrip("/"), old=job["name"])
        pipe(["sh", "-c", script], ["tar", "-xf", "-", "-C", job["backup"]], logfile=ui.LOG)
        if not os.path.getsize(f"{job['backup']}{CONFIG_DB}"):
            die("the backup of config.db is empty; nothing was changed")
        line.ok(f"backed up to {job['backup']}")


def without_host(text, names):
    """known_hosts text without the lines whose host field names one of names."""
    keep = [l for l in text.splitlines(keepends=True)
            if not (l.split() and set(l.split()[0].split(",")) & set(names))]
    return "".join(keep)


def forget_keys(job):
    names = [n for n in (job["name"], job["addr"]) if n]
    try:
        text = open(KNOWN_HOSTS).read()
    except OSError:
        return
    new = without_host(text, names)
    if new != text:
        replace_file(KNOWN_HOSTS, new, 0o600)


def node_remove(name, dry=False):
    job = survey(name)
    found = problems(job)
    if found:
        die(f"can't remove {name}; nothing was changed:\n" + "\n".join(f"  - {p}" for p in found))
    show_plan(job, dry)
    if dry:
        print("dry run - nothing was changed")
        return
    back_up(job)
    with Spinner(f"removing {name} from the cluster (pvecm delnode)") as line:
        delnode(name)
        line.ok(f"{name} removed from the cluster")
    with Spinner(f"removing /etc/pve/nodes/{name} and its ssh host keys") as line:
        if os.path.isdir(node_dir(name)):
            shutil.rmtree(node_dir(name))
        forget_keys(job)
        line.ok(f"/etc/pve/nodes/{name} and its ssh host keys removed")
    if peers_after(job) is not None:
        with Spinner("SDN: the zone's VXLAN peers without it, applied on every host") as line:
            with sdn.session():
                sdn.ensure_zone()            # the peers from the hosts left in the cluster
                sdn.apply()
            line.ok(f"SDN zone {sdn.SDN_ZONE}: peers {(sdn.zone() or {}).get('peers')}")
    left = sorted(members())
    print(paint(f"\n{name} is out of the cluster; hosts: {', '.join(left)}", "green"))
    print(f"  backup: {job['backup']}" + (f"\n  log: {ui.LOG}" if ui.LOG else ""))
    print(paint(f"  never start {name} on this network as it is: reinstall it first", "yellow"))

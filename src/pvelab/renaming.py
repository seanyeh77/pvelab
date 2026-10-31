"""pvelab node rename OLD NEW: rename a PVE host of the cluster. Two ways (also in `pvelab help node`):

A host with no guests but pvelab's template copies, and not this one, leaves the cluster and joins again
under the new name (PVE's advice: rename a node with nothing on it):
  back up - delete its template copies - separate it the documented way (pvecm chapter: "Separate a Node
  Without Reinstalling") - pvecm delnode OLD here - new hostname, reboot - join as node add does - prepare.
A host with guests, or this one, is renamed in place (the manual procedure):
  back up - /etc/hostname, /etc/hosts - corosync.conf's name: and config_version - shut its guests down as
  its reboot would - move their configs to /etc/pve/nodes/NEW - reboot - then: pvecm updatecerts --force,
  checks, the VMs that ran are running, /etc/pve/nodes/OLD removed.
The guests are shut down before their configs move: at the reboot PVE's stopall shuts down only the guests
whose configs are under the host's own (old) name, so moved ones would be killed instead.

This host reboots under pvelab: STATE_FILE says what is left, and the next pvelab command finishes it.
pvelab's own state names no host (credentials.tsv, blocks.json and the nft files go by VMID and tap,
template copies' descriptions by VMID, the SDN zone's peers by IP), so none of it changes."""

import glob
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import time

from . import joining, nodes, ui
from .config import BACK_SECS, COROSYNC_CONF, HOST_LABEL, LAB_DIR, STORAGE_CFG, read_text
from .guest import logged_in
from .hosts import node_list
from .inventory import is_managed, read_conf, vm_names, vm_statuses
from .nodes import (cfs_quorate, corosync_blocks, GUEST_KINDS, in_cluster, members, offline_nodes, on_node, online_nodes, qm_argv,
                    quorate, this_node)
from .templates import source_of
from .ui import Board, paint, Plan, Spinner, step
from .util import confirm_yes, die, LabError, pipe, replace_file, run, wait_for, warn

STATE_FILE = f"{LAB_DIR}/rename-state.json"   # an unfinished rename (see resume)
HOST_COROSYNC = "/etc/corosync/corosync.conf"  # each host's copy of the cluster's COROSYNC_CONF (pmxcfs makes it)
CONFIG_DB = "/var/lib/pve-cluster/config.db"
ONBOOT_SECS = 180                             # after the reboot: onboot starts the VMs; then pvelab does
START_SECS = 600
DELNODE_SECS = 180     # pvecm delnode again for this long: a stale cfs lock lasts 120 s (pmxcfs)
NODE_FILES = ("config", "host.fw")            # the node's own settings (description, ACME, ...; firewall rules)

# What each host's backup holds (paths without the leading /). config.db is copied with sqlite3 .backup:
# pmxcfs keeps it in WAL mode, so a plain copy of the file may miss the newest changes.
BACKUP_SH = """set -e
d=$(mktemp -d); trap 'rm -rf "$d"' EXIT
mkdir -p "$d/var/lib/pve-cluster"
sqlite3 {db} ".backup '$d{db}'"
set --
for p in etc/corosync etc/pve/corosync.conf etc/hostname etc/hosts etc/pve/nodes/{old}; do
  if [ -e "/$p" ]; then set -- "$@" "$p"; fi
done
# pmxcfs files change all the time (lrm_status every 10 s, via a .tmp file): skip the temp files, and
# take tar's exit 1 ("file changed as we read it") as done - exit 2 is a real failure
tar -cf - --exclude='*.tmp.*' --warning=no-file-changed --warning=no-file-removed -C "$d" {dbrel} -C / "$@" \
  || [ $? -eq 1 ]
"""

# PVE's "Separate a Node Without Reinstalling" (pvecm chapter of the admin guide), on the leaving host.
SEPARATE_SH = """set -e
systemctl stop pve-cluster corosync
pmxcfs -l
rm -f /etc/pve/corosync.conf
rm -rf /etc/corosync/*
killall pmxcfs
while pidof pmxcfs >/dev/null; do sleep 1; done
systemctl start pve-cluster
rm -f /var/lib/corosync/*
"""

# Then its own copy of /etc/pve forgets the other hosts: their guest configs are still in it, and
# `pvecm add` refuses a host that has guests (Cluster/Setup.pm: assert_joinable reads every node's list).
# Only once it is surely out of the cluster: there, this would delete the class's VM configs.
FORGET_SH = """set -e
if [ -e /etc/pve/corosync.conf ] || systemctl is-active --quiet corosync; then
  echo "still in a cluster: nothing removed" >&2; exit 1
fi
for d in /etc/pve/nodes/*; do
  if [ "$d" != /etc/pve/nodes/{old} ]; then rm -rf "$d"; fi
done
"""


# --- pure helpers ---

def bad_name(name):
    """Why name can't be a PVE node name (JSONSchema.pm: pve_verify_node_name), or None."""
    if not HOST_LABEL.fullmatch(name or ""):
        return f"'{name}' is not a valid host name (letters, digits and '-', not at the ends; at most 63)"
    return None


def swap(text, old, new):
    """Every whole word old (alone or as the first part of old.domain) in text becomes new; the
    first field of a line (an address) and comments are left alone."""
    out = []
    for line in text.splitlines(keepends=True):
        body, mark, comment = line.partition("#")
        out.append(re.sub(rf"(?<=\s){re.escape(old)}(?=[.\s]|$)", new, body) + mark + comment)
    return "".join(out)


def new_hostname(text, old, new):
    """/etc/hostname with new instead of old (a domain part is kept)."""
    name = text.strip()
    return (new + name[len(old):] if name == old or name.startswith(old + ".") else new) + "\n"


def corosync_version(text):
    m = re.search(r"^\s*config_version:\s*(\d+)", text or "", re.M)
    return int(m.group(1)) if m else None


def node_block(text, name):
    """The text of corosync.conf's node { } block whose name: is name, or None."""
    for block in corosync_blocks(text):
        if re.search(rf"^\s*name:\s*{re.escape(name)}\s*$", block, re.M):
            return block
    return None


def rename_corosync(text, old, new):
    """corosync.conf with node old named new and config_version one up (corosync takes a new
    config only with a higher version)."""
    lines = re.findall(rf"^\s*name:\s*{re.escape(old)}\s*$", text, re.M)
    if len(lines) != 1 or corosync_version(text) is None:
        die(f"corosync.conf: expected one 'name: {old}' and a config_version")
    text = re.sub(rf"^(\s*name:\s*){re.escape(old)}(\s*)$", rf"\g<1>{new}\g<2>", text, flags=re.M)
    return re.sub(r"^(\s*config_version:\s*)\d+", lambda m: m.group(1) + str(corosync_version(text) + 1),
                  text, count=1, flags=re.M)


def parse_pvecm_nodes(text):
    """`pvecm nodes` -> the names in its membership table."""
    return re.findall(r"^\s*\d+\s+\d+\s+(\S+)", text or "", re.M)


def parse_cmap_version(text):
    """`corosync-cmapctl -g totem.config_version` ('totem.config_version (u64) = 3') -> 3, or None."""
    m = re.search(r"config_version\s*\(\w+\)\s*=\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def running_ids(qm, pct):
    """`qm list` and `pct list` -> the VMIDs that run. qm's NAME may hold a space ("VM 100"): its STATUS
    is the fourth field from the end (MEM, BOOTDISK, PID after it); pct's is the second (LOCK may be empty)."""
    rows = [(l.split(), -4, 6) for l in (qm or "").splitlines()] + [(l.split(), 1, 3) for l in (pct or "").splitlines()]
    return sorted(int(f[0]) for f, i, least in rows if len(f) >= least and f[0].isdigit() and f[i] == "running")


# --- the renamed host: reached the same way before and after its corosync name changes ---

def argv_for(job, node, *cmd):
    """cmd on node: on the renamed host through job["ssh"], or here when it is this host (job["local"]);
    else as usual. Never here for another host whose ssh is unknown."""
    if node == job["old"]:
        if job.get("ssh"):
            return [*job["ssh"], "--", shlex.join(map(str, cmd))]
        if not job.get("local"):
            die(f"no way to reach {node} over ssh (its IP was not known when the rename began)")
        return [str(c) for c in cmd]
    return on_node(node, *cmd)


def put(job, node, path, text):
    """Write text to path on node, all at once (a new file moved over the old one)."""
    run(*argv_for(job, node, "sh", "-c", f"cat > {path}.pvelab-new && mv {path}.pvelab-new {path}"), data=text)


def node_dir(name):
    return f"{nodes.NODES_DIR}/{name}"


def guest_confs(name):
    """[(kind, vmid)] of the guest configs in /etc/pve/nodes/name."""
    return sorted((f.split("/")[-2], int(os.path.basename(f)[:-5])) for f in nodes.guest_conf_files(node=name))


# --- what is there, and the checks: nothing is changed ---

def survey(old, new):
    """The job: the hosts, the renamed host's guests (which are template copies, which run), which way."""
    here = this_node()
    mem = members()
    m = mem.get(old) or {}
    job = {"old": old, "new": new, "this": here, "ip": (mem.get(here) or {}).get("ip"), "addr": m.get("ip"),
           "local": nodes.is_local(old), "cluster": in_cluster(),
           "hosts": online_nodes(), "step": "checks", "phase": "",
           "backup": f"{LAB_DIR}/rename-backup-{time.strftime('%Y%m%d-%H%M%S')}", "log": None,
           "guests": guest_confs(old) if old in mem else [], "copies": {}, "running": [], "students": {},
           "silent": [], "ssh": None}
    if not job["local"] and m.get("online") and m.get("ip"):
        # taken while old is a member under its old name: its IP and host key alias
        job["ssh"] = nodes.ssh_argv(old, shared=False)
    for kind, v in job["guests"]:
        conf = read_conf(v, f"{node_dir(old)}/{kind}/{v}.conf") or {}
        if kind == "qemu-server" and conf.get("template") == "1" and source_of(conf) is not None:
            job["copies"][v] = conf.get("name", "")
    job["mode"] = "rejoin" if not job["local"] and len(job["copies"]) == len(job["guests"]) else "inplace"
    return job


def look_inside(job):
    """In place: which VMs run (they restart), and who is logged in to pvelab's (the guest agent's who)."""
    ids = [v for k, v in job["guests"] if k == "qemu-server" and v not in job["copies"]]
    st = vm_statuses(ids)
    confs = {v: read_conf(v, f"{node_dir(job['old'])}/qemu-server/{v}.conf") or {} for v in ids}
    names = vm_names(ids, confs)
    job["running"] = [[v, names[v], confs[v].get("onboot") == "1"] for v in ids if st.get(v) == "running"]
    job["students"], job["silent"] = logged_in([v for v, _, _ in job["running"] if is_managed(confs[v])], names)


def problems(job):
    """What stops the rename (each with what to do); [] when it can go on."""
    old, new, mem = job["old"], job["new"], members()
    out = [why] if (why := bad_name(new)) else []
    if old not in mem:
        return out + [f"{old} is not a host of this cluster (pvelab node list)"]
    if new.lower() in {n.lower() for n in mem} | {this_node().lower()}:
        out.append(f"{new} is a host of this cluster already")
    if os.path.exists(node_dir(new)):
        out.append(f"/etc/pve/nodes/{new} exists already: remove it if it is a leftover")
    offline = offline_nodes()
    if offline:
        out.append(f"every host must be online: {', '.join(offline)} is not")
    if job["cluster"]:
        if not quorate():
            out.append("the cluster is not quorate (pvecm status)")
        block = node_block(read_text(COROSYNC_CONF), old) or ""
        ring = re.search(r"ring0_addr:\s*(\S+)", block)
        try:
            ipaddress.ip_address(ring.group(1) if ring else "")
        except ValueError:
            out.append(f"corosync.conf has no IP address as ring0_addr of {old}: change it to its IP first")
    if re.search(rf"^\s*nodes\s+.*\b{re.escape(old)}\b", read_text(STORAGE_CFG), re.M):
        out.append(f"storage.cfg limits a storage to {old} (nodes ...): add {new} there first")
    if not job["local"] and not job["ssh"] and old not in offline:
        out.append(f"{old} has no IP in /etc/pve/.members (pvecm status): pvelab can't reach it over ssh")
    if not offline and (job["local"] or job["ssh"]):
        hosts = run(*argv_for(job, old, "cat", "/etc/hosts"), check=False, quiet=True)
        if swap(hosts, old, new) == hosts:
            out.append(f"/etc/hosts on {old} does not name {old} (the line: IP {old}.domain {old})")
    return out


# --- the plan ---

def backup_hosts(job):
    return list(job["hosts"]) or [job["old"]]


HOSTS_WRITERS = ("grep -lsE '(>|tee( -a)?)[[:space:]]*/etc/hosts' /usr/local/sbin/* /usr/local/bin/* "
                 "/etc/systemd/system/*.service /etc/rc.local /etc/network/if-up.d/* 2>/dev/null "
                 "| xargs -r grep -lw {old}")    # only those with the old name written in them


def hosts_writers(job):
    """Files on OLD that write /etc/hosts themselves (a boot script with the old name in it rewrites the
    renamed host's /etc/hosts at boot: pve-cluster then can't resolve the new name and doesn't start)."""
    try:
        return run(*argv_for(job, job["old"], "sh", "-c", HOSTS_WRITERS.format(old=shlex.quote(job["old"]))),
                   check=False, quiet=True).split()
    except LabError:
        return []


def show_plan(job, dry=False):
    """The steps, the reboots, the VMs that restart, the students logged in, the backups."""
    old, new, b = job["old"], job["new"], job["backup"]
    plan = Plan()
    plan.do(f"back up {', '.join(backup_hosts(job))} to {b}/<host>/ (config.db, /etc/corosync, "
            f"corosync.conf, hostname, hosts, /etc/pve/nodes/{old})")
    if job["mode"] == "rejoin":
        if job["copies"]:
            plan.do(f"delete the template copies on {old}: " + ", ".join(f"{n} ({v})" for v, n in job["copies"].items())
                    + " (made again when needed)")
        plan.do(f"separate {old}: its corosync stopped and its cluster state removed (PVE's documented "
                "way), the other hosts' configs dropped from its copy of /etc/pve")
        plan.do(f"remove {old} from the cluster: pvecm delnode {old} on {job['this']} (pvecm expected 1 "
                "first if this host is left without quorum)")
        plan.do(f"{old}: hostname {new} in /etc/hostname and /etc/hosts (the domain is kept), reboot")
        plan.do(f"join {new} ({job['addr']}) again as node add does (checks, pvecm add: it asks for the "
                f"root password of {job['this']} unless {old}'s root key is trusted here)")
        plan.do(f"prepare {new} for pvelab (vendor-data, isolation, SDN peers), remove /etc/pve/nodes/{old}")
    else:
        others = [h for h in job["hosts"] if h != old]
        plan.do(f"{old}: hostname {new} in /etc/hostname and /etc/hosts (the domain is kept)")
        if job["cluster"]:
            plan.do(f"corosync.conf: node {old} named {new}, config_version up by one"
                    + (f" (it spreads to {', '.join(others)})" if others else ""))
        plan.do(f"shut down the guests running on {old}" + (f" ({len(job['running'])} VM(s))" if job["running"] else "")
                + ", as its reboot would; then check that none runs")
        plan.do(f"move {len(job['guests'])} guest config(s) from /etc/pve/nodes/{old} to /etc/pve/nodes/{new}"
                + " (and its node config and host.fw, if any)")
        plan.do(f"reboot {old}" + (" - this host: pvelab finishes the rename at the next pvelab command"
                                   if job["local"] else ""))
        plan.do(f"after the reboot: pvecm updatecerts --force, check that {new} holds the VMs and the cluster "
                f"is quorate, the VMs that ran are running again, remove /etc/pve/nodes/{old}")
    for f in job.get("hosts_writers", []):
        plan.warn(f"{old}: {f} writes /etc/hosts with the name {old} in it (at boot?): make it follow the "
                  f"hostname (e.g. $(hostname -s)), or it puts {old} back and pve-cluster won't start as {new}")
    plan.show() if dry else plan.ask()     # a dry run only shows
    print(f"Reboots: {old} ({job['addr'] or 'this host'})")
    if len(job["hosts"]) == 2:
        print(paint("  while it is down, the other host has no quorum (its VMs keep running)", "dim"))
    ran = [f"{n}" + ("" if onboot else " (no onboot: pvelab starts it)") for _, n, onboot in job["running"]]
    print(f"VMs that restart: {ui.some(ran, 8) if ran else 'none'}")
    if job["students"]:
        print(paint(f"Students logged in ({len(job['students'])} VMs):", "yellow"))
        for name, users in sorted(job["students"].items()):
            print(paint(f"  {name}: {', '.join(users)}", "yellow"))
    else:
        print("Students logged in: none")
    if job["silent"]:
        print(paint(f"  no answer from the guest agent of: {ui.some(job['silent'], 8)}", "dim"))
    print(paint("Run it inside tmux or screen: a dropped ssh session must not end it half-way "
                "(pvelab ignores the hangup, but its output has nowhere to go).", "yellow"))


# --- the steps ---

def back_up(job):
    """Each host's files into job["backup"]/<host>/, made on that host and copied here."""
    os.makedirs(job["backup"], mode=0o700, exist_ok=True)
    script = BACKUP_SH.format(db=CONFIG_DB, dbrel=CONFIG_DB.lstrip("/"), old=job["old"])
    for host in backup_hosts(job):
        dest = f"{job['backup']}/{host}"
        with Spinner(f"backing up {host}") as line:
            os.makedirs(dest, exist_ok=True)
            pipe(argv_for(job, host, "sh", "-c", script), ["tar", "-xf", "-", "-C", dest], logfile=ui.LOG)
            if not os.path.getsize(f"{dest}{CONFIG_DB}"):
                die(f"the backup of {host}'s config.db is empty")
            line.ok(f"{host} backed up to {dest}")


def set_hostname(job):
    old, new = job["old"], job["new"]
    with Spinner(f"{old}: hostname {new} in /etc/hostname and /etc/hosts") as line:
        hosts = run(*argv_for(job, old, "cat", "/etc/hosts"))
        name = run(*argv_for(job, old, "cat", "/etc/hostname"))
        put(job, old, "/etc/hosts", swap(hosts, old, new))
        put(job, old, "/etc/hostname", new_hostname(name, old, new))
        line.ok(f"{old}: /etc/hostname and /etc/hosts name {new} (from its next boot)")


COROSYNC_SECS = 120


def edit_corosync(job):
    """name: and config_version in /etc/pve/corosync.conf (a new file moved over it, as PVE's docs do;
    not again on a resume when it names NEW already); then wait until every host's /etc/corosync has it
    and its corosync runs with it (cmap's totem.config_version), and pvecm nodes lists NEW."""
    old, new = job["old"], job["new"]
    with Spinner(f"corosync.conf: {old} -> {new}") as line:
        text = read_text(COROSYNC_CONF)
        if node_block(text, new) and not node_block(text, old):
            want = corosync_version(text)    # a resume: written already
        else:
            text = rename_corosync(text, old, new)
            want = corosync_version(text)
            replace_file(COROSYNC_CONF, text)
        why = []

        def spread():
            for h in job["hosts"]:
                have = corosync_version(run(*argv_for(job, h, "cat", HOST_COROSYNC), check=False, quiet=True))
                if have != want:
                    why[:] = [f"{h}: {HOST_COROSYNC} has config_version {have}"]
                    return False
                have = parse_cmap_version(run(*argv_for(job, h, "corosync-cmapctl", "-g", "totem.config_version"),
                                              check=False, quiet=True))
                if have != want:
                    why[:] = [f"{h}: corosync runs with config_version {have}"]
                    return False
            listed = parse_pvecm_nodes(run("pvecm", "nodes", check=False, quiet=True))
            if new not in listed:
                why[:] = [f"pvecm nodes lists {', '.join(listed) or 'nothing'}, not {new}"]
                return False
            return True
        try:
            wait_for(f"corosync.conf version {want} on every host", spread, COROSYNC_SECS)
        except LabError as e:
            die(f"{e} ({why[0] if why else 'no answer'})")
        line.ok(f"corosync.conf names {new} (config_version {want}) on {', '.join(job['hosts'])}; corosync runs with it")


def running_guests(job):
    """The VMIDs of the guests (VMs and containers) that run on OLD now (qm list, pct list there)."""
    old = job["old"]
    return running_ids(run(*argv_for(job, old, "qm", "list"), quiet=True),
                       run(*argv_for(job, old, "pct", "list"), quiet=True))


def stop_guests(job):
    """What the reboot would do (pve-guests' stopall), while the configs are still under the old name -
    whenever any guest runs there; then none may run: their configs must not move under a running guest."""
    old = job["old"]
    with Spinner(f"shutting down the guests on {old}") as line:
        running = running_guests(job)
        if running:
            ui.logged(*argv_for(job, old, "pvesh", "--nooutput", "create", "/nodes/localhost/stopall"))
            left = running_guests(job)
            if left:
                die(f"still running on {old} after stopall: {', '.join(map(str, left))} - stop them "
                    f"(qm shutdown / pct shutdown there), then: pvelab node rename")
        line.ok(f"{len(running)} guest(s) on {old} shut down" if running else f"no guest runs on {old}")


def move_configs(job):
    """The guest configs from /etc/pve/nodes/OLD to NEW, one file at a time (pmxcfs can't rename a
    directory that has files), and the host key under the new name for pvelab's ssh after the reboot."""
    old, new = job["old"], job["new"]
    with Spinner(f"moving the guest configs to /etc/pve/nodes/{new}") as line:
        for kind in GUEST_KINDS:
            os.makedirs(f"{node_dir(new)}/{kind}", exist_ok=True)
        for kind, v in guest_confs(old):
            os.rename(f"{node_dir(old)}/{kind}/{v}.conf", f"{node_dir(new)}/{kind}/{v}.conf")
        copy_node_files(old, new)
        known = f"{node_dir(old)}/ssh_known_hosts"
        if os.path.exists(known):
            with open(f"{node_dir(new)}/ssh_known_hosts", "w") as f:
                f.write(re.sub(rf"^{re.escape(old)}(?=\s)", new, read_text(known), flags=re.M))
        if guest_confs(old) or len(guest_confs(new)) != len(job["guests"]):
            die(f"/etc/pve/nodes/{new} holds {len(guest_confs(new))} of {len(job['guests'])} guest configs")
        line.ok(f"{len(job['guests'])} guest config(s) in /etc/pve/nodes/{new}")


def copy_node_files(old, new):
    """The node's own settings (NODE_FILES) copied to NEW's dir where it has none yet; OLD keeps its
    copy until its dir goes (its firewall rules hold until the host takes the new name)."""
    for name in NODE_FILES:
        src, dst = f"{node_dir(old)}/{name}", f"{node_dir(new)}/{name}"
        if os.path.isfile(src) and not os.path.exists(dst):
            with open(dst, "w") as f:
                f.write(read_text(src))


BOOT_ID = "cat /proc/sys/kernel/random/boot_id"


def reboot(job):
    """Reboot OLD; its boot id is kept so the wait after can tell the new boot from the old one, and for
    another host this waits until it is really down (its ssh port answers a few seconds after the call)."""
    old = job["old"]
    job["boot_id"] = run(*argv_for(job, old, "sh", "-c", BOOT_ID), check=False, quiet=True).strip()
    save(job, job["phase"])
    print(paint(f"  rebooting {old}", "bold"))
    ui.log(f"rebooting {old} (boot {job['boot_id'] or '?'})")
    run(*argv_for(job, old, "systemctl", "reboot"), check=False, quiet=True)
    if job["addr"] and not job["local"]:
        with Spinner(f"waiting for {old} to go down"):
            wait_for(f"{old} down for its reboot", lambda: not joining.tcp_ok(job["addr"], 22), 180)


INPLACE_STEPS = ("hostname", "corosync", "stop", "move", "reboot")    # in_place's steps before the reboot


def inplace_steps(job):
    return [s for s in INPLACE_STEPS if s != "corosync" or job["cluster"]]


def in_place(job, k, n, start="hostname"):
    """In place, up to the reboot (and after it, when it is another host). start: the step to begin at (a
    resume after a stop; each is safe to run again: set_hostname writes the same files, edit_corosync sees
    NEW there already, stopall only stops what runs, move_configs moves what is left in nodes/OLD)."""
    todo = INPLACE_STEPS[INPLACE_STEPS.index(start):]
    if "hostname" in todo:
        k = next_step(job, "hostname", k, n, f"hostname of {job['old']}")
        set_hostname(job)
    if job["cluster"] and "corosync" in todo:
        k = next_step(job, "corosync", k, n, "corosync.conf")
        edit_corosync(job)
    if "stop" in todo:
        k = next_step(job, "stop", k, n, f"shut down the guests on {job['old']}")
        stop_guests(job)
    if "move" in todo:
        k = next_step(job, "move", k, n, f"guest configs to /etc/pve/nodes/{job['new']}")
        move_configs(job)
    k = next_step(job, "reboot", k, n, f"reboot {job['old']}")
    save(job, "reboot")
    if job["local"]:
        print(paint(f"\nThis host reboots now. When it is back, log in and run any pvelab command (pvelab node "
                    f"list): it finishes the rename (log: {ui.LOG}).", "yellow"))
        reboot(job)
        return False
    reboot(job)
    k = next_step(job, "after", k, n, f"after the reboot of {job['new']}")
    after_reboot(job)
    return True


def after_reboot(job):
    """The steps after the in-place reboot: quorum, certificates, the VMs where they belong, running."""
    old, new = job["old"], job["new"]
    with Spinner(f"waiting for {new} online" + (" in a quorate cluster" if job["cluster"] else "")) as line:
        wait_for(f"{new} online", lambda: nodes.is_online(new) and (not job["cluster"] or (quorate() and cfs_quorate()))
                 and hostname_of(job, new) == new, BACK_SECS)
        line.ok(f"{new} is online" + ("; the cluster is quorate" if job["cluster"] else ""))
    with Spinner(f"pvecm updatecerts --force on {new}") as line:
        ui.logged(*on_node(new, "pvecm", "updatecerts", "--force"))
        ui.logged(*on_node(new, "systemctl", "reload-or-restart", "pvedaemon", "pveproxy"))
        line.ok(f"{new}: certificates and ssh keys made for its new name")
    with Spinner("checking the cluster and the guest configs") as line:
        if job["cluster"]:
            listed = parse_pvecm_nodes(run("pvecm", "nodes", check=False, quiet=True))
            if new not in listed or old in listed:
                die(f"pvecm nodes lists {', '.join(listed) or 'nothing'}, not {new}")
        have = guest_confs(new)
        if have != sorted(map(tuple, job["guests"])) or guest_confs(old):
            die(f"/etc/pve/nodes/{new} holds {len(have)} guest configs, not {len(job['guests'])}; "
                f"/etc/pve/nodes/{old} holds {len(guest_confs(old))}")
        line.ok(f"{new} holds its {len(have)} guest config(s)" + ("; pvecm nodes lists it" if job["cluster"] else ""))
    start_vms(job)
    drop_old_dir(job)


def hostname_of(job, node):
    """node's short hostname (see argv_for); "" while it can't be reached (pmxcfs may list a host just back
    without its IP)."""
    try:
        return run(*argv_for(job, node, "hostname"), check=False, quiet=True).strip().split(".")[0]
    except LabError:
        return ""


def start_vms(job):
    """The VMs that ran before the reboot: onboot starts them; after ONBOOT_SECS pvelab starts the rest."""
    want = {int(v): name for v, name, _ in job["running"]}
    if not want:
        return
    left, t0, started = set(want), time.time(), False
    with Board(f"VMs on {job['new']}", want) as b:
        for v in want:
            b.stage(v, "waiting for onboot")
        while left and time.time() - t0 < START_SECS:
            st = vm_statuses(sorted(left))
            for v in sorted(v for v in left if st.get(v) == "running"):
                b.ok(v, "running")
                left.discard(v)
            if left and not started and time.time() - t0 >= ONBOOT_SECS:
                for v in sorted(left):
                    b.stage(v, "starting")
                    run(*qm_argv(v, "start", v), check=False, quiet=True)
                started = True
            if left:
                time.sleep(5)
        for v in sorted(left):
            b.fail(v, "not running")
    job["not_started"] = [want[v] for v in sorted(left)]


def drop_old_dir(job):
    """/etc/pve/nodes/OLD once no guest config is left in it (its certificates are in the backup)."""
    old = job["old"]
    if not os.path.isdir(node_dir(old)) or old in members():
        return
    if guest_confs(old):
        warn(f"/etc/pve/nodes/{old} still holds guest configs: left as it is")
        return
    if os.path.isdir(node_dir(job["new"])):
        copy_node_files(old, job["new"])
    certs = sorted(os.path.basename(f) for f in glob.glob(f"{node_dir(old)}/pveproxy-ssl.*"))
    shutil.rmtree(node_dir(old))
    print(f"  {paint(ui.OK_MARK, 'green')} /etc/pve/nodes/{old} removed (a copy is in {job['backup']})")
    if certs:                                 # made for the old name: not carried over
        warn(f"{old}'s own web certificate ({', '.join(certs)}) was not kept: it is in "
             f"{job['backup']}/{old}/etc/pve/nodes/{old}/ (upload a new one for {job['new']})")


LEAVE_STEPS = ("copies", "separate", "delnode", "hostname")   # leave_rejoin's steps before the join


def leave_rejoin(job, k, n, start="copies"):
    """A host with nothing on it: out of the cluster, renamed, in again. start: the step to begin at (a
    resume after a stop; the steps are safe to run again: delnode waits until OLD is gone)."""
    old, new = job["old"], job["new"]
    todo = LEAVE_STEPS[LEAVE_STEPS.index(start):]
    if "copies" in todo and job["copies"]:
        k = next_step(job, "copies", k, n, f"template copies on {old}")
        for v, name in job["copies"].items():
            with Spinner(f"deleting the copy {name} ({v}) on {old}") as line:
                ui.logged(*argv_for(job, old, "qm", "destroy", v, "--purge", "1"))
                line.ok(f"copy {name} ({v}) deleted on {old}")
    if "separate" in todo:
        k = next_step(job, "separate", k, n, f"separate {old}")
        with Spinner(f"separating {old} (its cluster state)") as line:
            ui.logged(*argv_for(job, old, "sh", "-c", SEPARATE_SH))
            ui.logged(*argv_for(job, old, "sh", "-c", FORGET_SH.format(old=old)))
            line.ok(f"{old} is on its own, with no other host's configs")
    if "delnode" in todo:
        k = next_step(job, "delnode", k, n, f"remove {old} from the cluster")
        with Spinner(f"pvecm delnode {old}") as line:
            if old in members():             # a resume: delnode may have finished already
                delnode(old)
            line.ok(f"{old} removed from the cluster; {job['this']} is quorate")
    k = next_step(job, "hostname", k, n, f"rename {old} and reboot it")
    set_hostname(job)
    save(job, "rejoin")
    reboot(job)
    k = next_step(job, "join", k, n, f"join {new} again")
    rejoin(job)
    return True


def delnode(old):
    """pvecm delnode OLD once OLD is separated (its corosync stopped), as PVE's docs order it (pvecm
    chapter, "Separate a Node Without Reinstalling"); then wait until it is gone. With two hosts this one
    has lost quorum by then: pvecm expected 1 first, and wait until pmxcfs (not only corosync) is quorate.
    delnode again for a while on failure: a delnode cut off half-way leaves a cfs lock for 120 s."""
    if not quorate():
        ui.logged("pvecm", "expected", "1")
    wait_for("/etc/pve quorate", lambda: quorate() and cfs_quorate(), 60)
    t0 = time.time()
    while node_block(read_text(COROSYNC_CONF), old):
        try:
            ui.logged("pvecm", "delnode", old)
            break
        except LabError as e:                 # "no quorum?" or "got lock request timeout"
            if time.time() - t0 > DELNODE_SECS:
                raise
            ui.log(f"pvecm delnode {old}: {e} - trying again")
            time.sleep(10)
    wait_for(f"{old} gone from the cluster", lambda: old not in members() and quorate(), 120)


def rejoin(job):
    """Once the host is back as NEW: node add's checks and join, then its verify and prepare. NEW a
    member already (a resume after the join worked): straight to verify and prepare, as node add's rerun."""
    new = job["new"]
    job["step"] = "join"
    if new not in members():
        join_again(job)
    job["step"] = "verify"
    joining.verify(new)
    job["step"] = "prepare"
    joining.prepare(new)
    drop_old_dir(job)


def join_again(job):
    """Wait for the host back as NEW, then node add's checks and join over one ssh connection."""
    new = job["new"]
    # its API (8006) starts well after ssh: node add's port check would fail if run before it
    with Spinner(f"waiting for {job['addr']} to be back (ssh and API ports)") as line:
        wait_for(f"{job['addr']} back", lambda: all(joining.tcp_ok(job["addr"], p) for p in (22, 8006)),
                 BACK_SECS)
        line.ok(f"{job['addr']} answers on its ssh and API ports")
    remote = open_remote(job)                # out of the cluster, root's cluster key may no longer work
    try:
        boot, _, name = remote.run(f"{BOOT_ID}; hostname")[1].strip().partition("\n")
        name = name.strip().split(".")[0]
        if job.get("boot_id") and boot.strip() == job["boot_id"]:
            die(f"{job['addr']} has not rebooted yet (the same boot as before): wait, then pvelab node rename")
        if name != new:
            die(f"{job['addr']} is {name or '?'} after its reboot, not {new} (/etc/hostname there?)")
        job["name"] = new
        failed = joining.run_checks(remote, joining.local_facts(job))
        if failed:
            die(f"{len(failed)} check(s) failed: fix them as shown above, then: pvelab node add {job['addr']}")
        try:
            joining.join(remote, job)
        except LabError:
            job["step"] = "join"              # node add's "nothing changed yet" is not true here
            raise
    finally:
        remote.close()


def open_remote(job):
    """The host out of the cluster: root ssh with the cluster's key while it still works, else as node
    add does (asks for the user; ssh asks for the password)."""
    remote = joining.Remote(job["addr"], "root", nodes.known_hosts_opts(job["old"]))
    if remote.open(batch=True):
        return remote
    return joining.connect(job["addr"])


# --- state: an unfinished rename survives the reboot ---

def save(job, phase):
    job["phase"] = phase
    replace_file(STATE_FILE, json.dumps(job, indent=1), 0o600)


def pending():
    """Whether a rename is unfinished (this host rebooted under it, or it stopped half-way)."""
    return os.path.exists(STATE_FILE)


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        die(f"can't read {STATE_FILE}: {e}")


def clear():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def next_step(job, name, k, n, title):
    """Step k of n; once something changes, the state file follows each step (for resume and recovery)."""
    job["step"] = name
    if job["phase"]:
        save(job, job["phase"])
    step(k, n, title)
    return k + 1


def step_count(job):
    if job["mode"] == "rejoin":
        return 5 + bool(job["copies"])
    return 1 + len(inplace_steps(job)) + (not job["local"])


# --- what went wrong, and the way back ---

UNCHANGED = ("checks", "confirm", "backup")
CONSISTENT = ("copies",)                      # only template copies gone: the cluster is as it was
STATE = {
    "checks": "nothing was changed",
    "confirm": "nothing was changed",
    "backup": "nothing was changed (the backup in {backup} may be partial)",
    "hostname": "{old}: /etc/hostname and /etc/hosts may name {new} already (from its next boot); the rest unchanged"
                " (to undo just that, on {old}: cp {backup}/{old}/etc/hostname {backup}/{old}/etc/hosts /etc/)",
    "rehost": "{old} ({addr}): out of the cluster, on its own; /etc/hostname and /etc/hosts may name {new} already",
    "corosync": "{old}: its hostname files name {new}; corosync.conf may name it {new} too (grep name: {conf})",
    "stop": "{old}: renamed in its files and corosync.conf; its guests may be shut down",
    "move": "{old}: renamed in its files and corosync.conf; its guest configs are in /etc/pve/nodes/{old} "
            "and /etc/pve/nodes/{new}",
    "reboot": "everything is changed; {old} needs its reboot (systemctl reboot there), then: pvelab node rename",
    "after": "{new} rebooted with its new name; the steps after it did not finish: run pvelab node rename again",
    "copies": "{old}: still in the cluster; some of its template copies may be deleted (made again when "
              "needed); nothing else changed: pvelab node rename {old} {new} may run again",
    "separate": "{old} ({addr}): still in this cluster's config; its own cluster state may be half "
                "removed: pvelab node rename {old} {new} goes on from there",
    "delnode": "{old} ({addr}): separated; may be removed from the cluster already (pvecm nodes). If "
               "this host has no quorum (pvecm status): pvecm expected 1, then pvecm delnode {old} again",
    "join": "{addr}: out of the cluster, named {new} after its reboot. To finish: pvelab node add {addr}",
    "verify": "{addr} ({new}): joined, not yet seen online and quorate. To finish: pvelab node rename",
    "prepare": "{new} is in the cluster; pvelab's setup on it is not finished. To finish: pvelab node rename",
}


def restore_steps(job):
    """How to go back to the state of the backup: each stage on every host before the next stage (pmxcfs
    and corosync must not run anywhere while config.db is put back, or a host still running syncs the
    changed state back); only the renamed host reboots."""
    b, db, old = job["backup"], CONFIG_DB, job["old"]
    return "\n".join([
        f"  to undo it all, from the backup in {b} (copy {b}/<host> to each host it has first, if it is",
        f"  not {job['this']}), as root - each step on every host before the next step:",
        "    1. on every host: systemctl stop pve-cluster corosync",
        f"    2. on every host: rm -f {db}-wal {db}-shm; cp {b}/<host>{db} {db}; chmod 600 {db}",
        f"                      cp -a {b}/<host>/etc/corosync/. /etc/corosync/     (if it has one)",
        "    3. on every host: systemctl start corosync pve-cluster",
        f"    4. on {old} (the renamed host) only: cp {b}/{old}/etc/hostname {b}/{old}/etc/hosts /etc/; reboot",
        f"  then remove {STATE_FILE} on {job['this']}"])


def failure(job, e):
    step_ = job["step"] if job["step"] in STATE else "join"
    if step_ == "hostname" and job["mode"] == "rejoin":      # after the separation: not only the files
        step_ = "rehost"
    text = STATE[step_].format(conf=COROSYNC_CONF, **job)
    msg = f"node rename: step '{job['step']}' failed: {str(e) or 'interrupted'}\n  state: {text}"
    if job["step"] in UNCHANGED + CONSISTENT:
        clear()
    else:
        msg += "\n" + restore_steps(job)
        if job["step"] in (LEAVE_STEPS if job["mode"] == "rejoin" else INPLACE_STEPS[:-1]):
            msg += "\n  or go on from there: pvelab node rename (each step is safe to run again)"
    if pending():                             # cli.py runs resume() before each command that changes something
        msg += (f"\n  while {STATE_FILE} is there, each pvelab command that changes something first tries to"
                f"\n  finish this rename, and stops if it can't. Not finishing it: once the hosts are restored or"
                f"\n  put right by hand, rm {STATE_FILE}")
    return msg + (f"\n  log: {ui.LOG}" if ui.LOG else "")


# --- the command ---

def node_rename(old, new, dry=False):
    """`node rename OLD NEW`, or alone: finish the unfinished one. A hangup (the ssh session dropped) is
    ignored while it runs: half-way through, the rename must go on to a step it can resume from."""
    if dry:
        return rename(old, new, dry)
    hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        return rename(old, new, dry)
    finally:
        signal.signal(signal.SIGHUP, hup)


def rename(old, new, dry=False):
    if pending():
        if dry:
            die(f"a rename is unfinished ({STATE_FILE}): run pvelab node rename to finish it")
        job = load()
        if old and new and new != job["new"]:
            switch_name(job, old, new)
        return resume()
    job = survey(old, new)
    bad = problems(job)
    if bad:
        for p in bad:
            print(f"  {paint(ui.BAD_MARK, 'red')} {p}")
        die(f"{len(bad)} problem(s): nothing was changed")
    if job["mode"] == "inplace":
        look_inside(job)
    job["hosts_writers"] = hosts_writers(job)
    print(f"Rename {old} to {new}: " + ("it leaves the cluster and joins again under the new name (no guests on it)"
                                       if job["mode"] == "rejoin" else "in place (it reboots)"))
    show_plan(job, dry)
    if dry:
        print("dry run - nothing was changed")
        return
    job["log"] = ui.LOG                       # one log for the whole rename, also the part after a reboot
    job["step"] = "confirm"
    confirm_yes("", typed=True)
    n = step_count(job)
    try:
        k = next_step(job, "backup", 1, n, "backups")
        back_up(job)
        save(job, "changing")
        finished = (leave_rejoin if job["mode"] == "rejoin" else in_place)(job, k, n)
    except (LabError, KeyboardInterrupt, OSError) as e:
        die(failure(job, e))
    if finished:
        done(job)


def switch_name(job, old, new):
    """`node rename OLD NEW` while the rename of OLD is unfinished: take NEW instead, if OLD's hostname is
    not changed yet (a leave-and-rejoin that stopped before its hostname step)."""
    if old != job["old"] or job["mode"] != "rejoin" or job["step"] not in ("copies", "delnode", "separate"):
        die(f"a rename of {job['old']} to {job['new']} is unfinished (step {job['step']}): finish it first with "
            f"pvelab node rename")
    if (why := bad_name(new)) or new in members():
        die(why or f"{new} is already a host of this cluster")
    print(paint(f"pvelab: renaming {old} to {new} instead of {job['new']} (its hostname is not changed yet)", "bold"))
    job["new"] = new
    save(job, job["phase"])


def resume(auto=False):
    """Finish the rename in STATE_FILE: the steps after an in-place reboot, or the join after a rejoin's;
    a leave-and-rejoin that stopped before its join, or an in-place rename that stopped before its reboot,
    goes on from there, but only when asked for (`node rename`), not as a side effect of another command
    (auto). Anything else: where it is, how to undo it."""
    job = load()
    ui.LOG = job.get("log") or ui.LOG         # the rename's log (node add's helpers write to it too)
    print(paint(f"pvelab: finishing the rename of {job['old']} to {job['new']} (log: {ui.LOG})", "bold"))
    try:
        if job["phase"] == "reboot" and job["mode"] == "inplace":
            if job["local"] and this_node() != job["new"]:
                die(f"this host is still {this_node()}: reboot it (systemctl reboot) to finish")
            job["step"] = "after"
            step(1, 1, f"after the reboot of {job['new']}")
            after_reboot(job)
        elif job["phase"] == "rejoin":
            step(1, 1, f"join {job['new']} again")
            rejoin(job)
        elif job["mode"] == "rejoin" and job["step"] in LEAVE_STEPS and not auto:
            n = step_count(job)              # go on from the step that stopped (each is safe to run again)
            k = n - 3 + LEAVE_STEPS[1:].index(job["step"]) if job["step"] != "copies" else 2
            leave_rejoin(job, k, n, start=job["step"])
        elif job["mode"] == "inplace" and job["step"] in INPLACE_STEPS and not auto:
            # go on from the step that stopped; a stop in "move" checks first that no guest runs
            start = "stop" if job["step"] == "move" else job["step"]
            n = step_count(job)
            if not in_place(job, 2 + inplace_steps(job).index(start), n, start=start):
                return                       # this host reboots: the next pvelab command finishes it
        else:
            go_on = job["step"] in (LEAVE_STEPS if job["mode"] == "rejoin" else INPLACE_STEPS)
            die(f"it stopped at step '{job['step']}'" + ("; to go on from there: pvelab node rename" if go_on else ""))
    except (LabError, KeyboardInterrupt, OSError) as e:
        die(failure(job, e))
    done(job)


def done(job):
    """The end: old and new name, the host table, VMs that did not start, the log."""
    clear()
    print(paint(f"\n{job['old']} is now {job['new']}.\n", "green"))
    node_list()
    if job.get("not_started"):
        print(paint(f"\nnot running after the rename: {ui.some(job['not_started'], 8)} - see pvelab list", "red"))
    print(paint(f"\nbackup: {job['backup']}\nlog: {ui.LOG}", "dim"))

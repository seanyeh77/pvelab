"""pvelab node rename OLD NEW: rename a PVE host of the cluster. Two ways (also in `pvelab help node`):

A host with no guests but pvelab's template copies, and not this one, leaves the cluster and joins again
under the new name (PVE's advice: rename a node with nothing on it):
  back up - delete its template copies - pvecm delnode OLD here - separate it the documented way (pvecm
  chapter: "Separate a Node Without Reinstalling") - new hostname, reboot - join as node add does - prepare.
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
import time

from . import joining, nodes, ui
from .config import BACK_SECS, LAB_DIR, STORAGE_CFG, read_text
from .guest import who_on
from .inventory import is_managed, read_conf, vm_statuses
from .joining import corosync_blocks, COROSYNC_CONF, quorate
from .nodes import members, offline_nodes, on_node, online_nodes, qm_argv, this_node
from .templates import source_of, wait_for
from .ui import Board, paint, Plan, Spinner, step
from .util import confirm_yes, die, LabError, pipe, run, warn

STATE_FILE = f"{LAB_DIR}/rename-state.json"   # an unfinished rename (see resume)
HOST_COROSYNC = "/etc/corosync/corosync.conf"  # each host's copy of the cluster's COROSYNC_CONF (pmxcfs makes it)
CONFIG_DB = "/var/lib/pve-cluster/config.db"
ONBOOT_SECS = 180                             # after the reboot: onboot starts the VMs; then pvelab does
START_SECS = 600
KINDS = ("qemu-server", "lxc")                # the guest config dirs of a node
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
tar -cf - -C "$d" {dbrel} -C / "$@"
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
    if not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?", name or "") or len(name) > 63:
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


# --- the renamed host: reached the same way before and after its corosync name changes ---

def argv_for(job, node, *cmd):
    """cmd on node: on the renamed host through job["ssh"] (None: it is this host), else as usual."""
    if node == job["old"]:
        return [*job["ssh"], "--", shlex.join(map(str, cmd))] if job["ssh"] else [str(c) for c in cmd]
    return on_node(node, *cmd)


def put(job, node, path, text):
    """Write text to path on node, all at once (a new file moved over the old one)."""
    run(*argv_for(job, node, "sh", "-c", f"cat > {path}.pvelab-new && mv {path}.pvelab-new {path}"), data=text)


def node_dir(name):
    return f"{nodes.NODES_DIR}/{name}"


def guest_confs(name):
    """[(kind, vmid)] of the guest configs in /etc/pve/nodes/name."""
    return sorted((k, int(os.path.basename(f)[:-5])) for k in KINDS
                  for f in glob.glob(f"{node_dir(name)}/{k}/*.conf"))


# --- what is there, and the checks: nothing is changed ---

def survey(old, new):
    """The job: the hosts, the renamed host's guests (which are template copies, which run), which way."""
    here = this_node()
    mem = members()
    m = mem.get(old) or {}
    job = {"old": old, "new": new, "this": here, "ip": (mem.get(here) or {}).get("ip"), "addr": m.get("ip"),
           "local": nodes.is_local(old), "cluster": os.path.exists(COROSYNC_CONF),
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
    st = vm_statuses(ids) if ids else {}
    confs = {v: read_conf(v, f"{node_dir(job['old'])}/qemu-server/{v}.conf") or {} for v in ids}
    job["running"] = [[v, confs[v].get("name", str(v)), confs[v].get("onboot") == "1"]
                      for v in ids if st.get(v) == "running"]
    found = who_on([v for v, _, _ in job["running"] if is_managed(confs[v])])
    job["students"] = {confs[v].get("name", str(v)): u for v, u in found.items() if u}
    job["silent"] = [confs[v].get("name", str(v)) for v, u in found.items() if u is None]


def problems(job):
    """What stops the rename (each with what to do); [] when it can go on."""
    old, new, mem = job["old"], job["new"], members()
    out = [bad_name(new)] if bad_name(new) else []
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
    if not offline and (job["local"] or job["ssh"]):
        hosts = run(*argv_for(job, old, "cat", "/etc/hosts"), check=False, quiet=True)
        if swap(hosts, old, new) == hosts:
            out.append(f"/etc/hosts on {old} does not name {old} (the line: IP {old}.domain {old})")
    return out


# --- the plan ---

def backup_hosts(job):
    return list(job["hosts"]) or [job["old"]]


def show_plan(job):
    """The steps, the reboots, the VMs that restart, the students logged in, the backups."""
    old, new, b = job["old"], job["new"], job["backup"]
    plan = Plan()
    plan.do(f"back up {', '.join(backup_hosts(job))} to {b}/<host>/ (config.db, /etc/corosync, "
            f"corosync.conf, hostname, hosts, /etc/pve/nodes/{old})")
    if job["mode"] == "rejoin":
        if job["copies"]:
            plan.do(f"delete the template copies on {old}: " + ", ".join(f"{n} ({v})" for v, n in job["copies"].items())
                    + " (made again when needed)")
        plan.do(f"remove {old} from the cluster: pvecm delnode {old} on {job['this']}")
        plan.do(f"separate {old}: its own cluster state removed (PVE's documented way), the other hosts' "
                "configs dropped from its copy of /etc/pve")
        plan.do(f"{old}: hostname {new} in /etc/hostname and /etc/hosts (the domain is kept), reboot")
        plan.do(f"join {new} ({job['addr']}) again as node add does (checks, pvecm add: it asks for the "
                f"root password of {job['this']})")
        plan.do(f"prepare {new} for pvelab (vendor-data, isolation, SDN peers), remove /etc/pve/nodes/{old}")
    else:
        others = [h for h in job["hosts"] if h != old]
        plan.do(f"{old}: hostname {new} in /etc/hostname and /etc/hosts (the domain is kept)")
        if job["cluster"]:
            plan.do(f"corosync.conf: node {old} named {new}, config_version up by one"
                    + (f" (it spreads to {', '.join(others)})" if others else ""))
        if job["running"]:
            plan.do(f"shut down the {len(job['running'])} running VM(s) on {old}, as its reboot would")
        plan.do(f"move {len(job['guests'])} guest config(s) from /etc/pve/nodes/{old} to /etc/pve/nodes/{new}"
                + " (and its node config and host.fw, if any)")
        plan.do(f"reboot {old}" + (" - this host: pvelab finishes the rename at the next pvelab command"
                                   if job["local"] else ""))
        plan.do(f"after the reboot: pvecm updatecerts --force, check that {new} holds the VMs and the cluster "
                f"is quorate, the VMs that ran are running again, remove /etc/pve/nodes/{old}")
    plan.ask(die)
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


def edit_corosync(job):
    """name: and config_version in /etc/pve/corosync.conf (a new file moved over it, as PVE's docs do);
    then wait until every host's /etc/corosync has it."""
    old, new = job["old"], job["new"]
    with Spinner(f"corosync.conf: {old} -> {new}") as line:
        text = rename_corosync(read_text(COROSYNC_CONF), old, new)
        want = corosync_version(text)
        with open(COROSYNC_CONF + ".new", "w") as f:
            f.write(text)
        os.replace(COROSYNC_CONF + ".new", COROSYNC_CONF)
        wait_for(f"corosync.conf version {want} on every host",
                 lambda: all(corosync_version(run(*argv_for(job, h, "cat", HOST_COROSYNC), check=False, quiet=True))
                             == want for h in job["hosts"]), 120)
        line.ok(f"corosync.conf names {new} (config_version {want}) on {', '.join(job['hosts'])}")


def stop_guests(job):
    """What the reboot would do (pve-guests' stopall), while the configs are still under the old name."""
    with Spinner(f"shutting down the VMs on {job['old']}") as line:
        joining.logged(*argv_for(job, job["old"], "pvesh", "--nooutput", "create", "/nodes/localhost/stopall"))
        line.ok(f"{len(job['running'])} VM(s) on {job['old']} shut down")


def move_configs(job):
    """The guest configs from /etc/pve/nodes/OLD to NEW, one file at a time (pmxcfs can't rename a
    directory that has files), and the host key under the new name for pvelab's ssh after the reboot."""
    old, new = job["old"], job["new"]
    with Spinner(f"moving the guest configs to /etc/pve/nodes/{new}") as line:
        for kind in KINDS:
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


def reboot(job):
    print(paint(f"  rebooting {job['old']}", "bold"))
    ui.log(f"rebooting {job['old']}")
    run(*argv_for(job, job["old"], "systemctl", "reboot"), check=False, quiet=True)


def in_place(job, k, n):
    """In place, up to the reboot (and after it, when it is another host)."""
    k = next_step(job, "hostname", k, n, f"hostname of {job['old']}")
    set_hostname(job)
    if job["cluster"]:
        k = next_step(job, "corosync", k, n, "corosync.conf")
        edit_corosync(job)
    if job["running"]:
        k = next_step(job, "stop", k, n, f"shut down the VMs on {job['old']}")
        stop_guests(job)
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
        wait_for(f"{new} online", lambda: members().get(new, {}).get("online") and (not job["cluster"] or quorate())
                 and hostname_of(job, new) == new, BACK_SECS)
        line.ok(f"{new} is online" + ("; the cluster is quorate" if job["cluster"] else ""))
    with Spinner(f"pvecm updatecerts --force on {new}") as line:
        joining.logged(*on_node(new, "pvecm", "updatecerts", "--force"))
        joining.logged(*on_node(new, "systemctl", "reload-or-restart", "pvedaemon", "pveproxy"))
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


def leave_rejoin(job, k, n):
    """A host with nothing on it: out of the cluster, renamed, in again."""
    old, new = job["old"], job["new"]
    if job["copies"]:
        k = next_step(job, "copies", k, n, f"template copies on {old}")
        for v, name in job["copies"].items():
            with Spinner(f"deleting the copy {name} ({v}) on {old}") as line:
                joining.logged(*argv_for(job, old, "qm", "destroy", v, "--purge", "1"))
                line.ok(f"copy {name} ({v}) deleted on {old}")
    k = next_step(job, "delnode", k, n, f"remove {old} from the cluster")
    with Spinner(f"pvecm delnode {old}") as line:
        delnode(old)
        line.ok(f"{old} removed from the cluster; {job['this']} is quorate")
    k = next_step(job, "separate", k, n, f"separate {old}")
    with Spinner(f"separating {old} (its cluster state)") as line:
        joining.logged(*argv_for(job, old, "sh", "-c", SEPARATE_SH))
        joining.logged(*argv_for(job, old, "sh", "-c", FORGET_SH.format(old=old)))
        line.ok(f"{old} is on its own, with no other host's configs")
    k = next_step(job, "hostname", k, n, f"rename {old} and reboot it")
    set_hostname(job)
    save(job, "rejoin")
    reboot(job)
    k = next_step(job, "join", k, n, f"join {new} again")
    rejoin(job)
    return True


def delnode(old):
    """pvecm delnode OLD, then wait until it is gone and this host quorate. PVE's delnode stops corosync
    on OLD before it writes the new corosync.conf: with two hosts this one may lose quorum first. Then,
    as PVE's docs say (pvecm chapter, "Separate a Node Without Reinstalling"): pvecm expected 1, again."""
    gone = lambda: old not in members() and quorate()
    try:
        joining.logged("pvecm", "delnode", old)
        wait_for(f"{old} gone from the cluster", gone, 60)
        return
    except LabError as e:
        ui.log(f"pvecm delnode {old}: {e} - now: pvecm expected 1, and delnode again")
    joining.logged("pvecm", "expected", "1")
    if node_block(read_text(COROSYNC_CONF), old):         # not written yet
        joining.logged("pvecm", "delnode", old)
    wait_for(f"{old} gone from the cluster", gone, 120)


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
    old, new = job["old"], job["new"]
    with Spinner(f"waiting for {job['addr']} to be back as {new}") as line:
        wait_for(f"{job['addr']} back as {new}", lambda: hostname_of(job, old) == new, BACK_SECS)
        line.ok(f"{job['addr']} is back as {new}")
    remote = open_remote(job)
    try:
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
    known = f"{node_dir(job['old'])}/ssh_known_hosts"
    opts = (["-o", f"HostKeyAlias={job['old']}", "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=none"]
            if os.path.isfile(known) else [])
    remote = joining.Remote(job["addr"], "root", opts)
    if remote.open(batch=True):
        return remote
    user = joining.ask_user(job["addr"])
    remote = joining.Remote(job["addr"], user)
    joining.hint(f"the ssh password of {user}@{job['addr']} (not asked if it has your key)")
    remote.open()
    return remote


# --- state: an unfinished rename survives the reboot ---

def save(job, phase):
    job["phase"] = phase
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


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
    return 4 + job["cluster"] + bool(job["running"]) + (not job["local"])


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
    "stop": "{old}: renamed in its files and corosync.conf; its VMs may be shut down",
    "move": "{old}: renamed in its files and corosync.conf; its guest configs are in /etc/pve/nodes/{old} "
            "and /etc/pve/nodes/{new}",
    "reboot": "everything is changed; {old} needs its reboot (systemctl reboot there), then: pvelab node rename",
    "after": "{new} rebooted with its new name; the steps after it did not finish: run pvelab node rename again",
    "copies": "{old}: still in the cluster; some of its template copies may be deleted (made again when "
              "needed); nothing else changed: pvelab node rename {old} {new} may run again",
    "delnode": "{old}: may be removed from the cluster already (pvecm nodes). If this host has no quorum "
               "(pvecm status): pvecm expected 1, then pvecm delnode {old} again",
    "separate": "{old} ({addr}): removed from the cluster; its own cluster state may be half removed",
    "join": "{addr}: out of the cluster, named {new} after its reboot. To finish: pvelab node add {addr}",
    "verify": "{addr} ({new}): joined, not yet seen online and quorate. To finish: pvelab node rename",
    "prepare": "{new} is in the cluster; pvelab's setup on it is not finished. To finish: pvelab node rename",
}


def restore_steps(job):
    """How to go back to the state of the backup, host by host."""
    b, db = job["backup"], CONFIG_DB
    return "\n".join([
        f"  to undo it all, from the backup in {b}: on each host it has (copy {b}/<host> there first if it",
        f"  is not {job['this']}), as root, at about the same time:",
        "      systemctl stop pve-cluster corosync",
        f"      rm -f {db}-wal {db}-shm; cp {b}/<host>{db} {db}; chmod 600 {db}",
        f"      cp -a {b}/<host>/etc/corosync/. /etc/corosync/     (if it has one)",
        f"      cp {b}/<host>/etc/hostname {b}/<host>/etc/hosts /etc/",
        "      reboot",
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
    if pending():                             # cli.py runs resume() before each command that changes something
        msg += (f"\n  while {STATE_FILE} is there, each pvelab command that changes something first tries to"
                f"\n  finish this rename, and stops if it can't. Not finishing it: once the hosts are restored or"
                f"\n  put right by hand, rm {STATE_FILE}")
    return msg + (f"\n  log: {ui.LOG}" if ui.LOG else "")


# --- the command ---

def use_log(path=None):
    """One log for the whole rename (also the part after a reboot): path, the log of the rename that
    is finished now; else this command's log. Node add's helpers write to it too."""
    if path:
        ui.LOG = path
    return ui.LOG


def node_rename(old, new, dry=False):
    if pending():
        if dry:
            die(f"a rename is unfinished ({STATE_FILE}): run pvelab node rename to finish it")
        return resume()
    job = survey(old, new)
    bad = problems(job)
    if bad:
        for p in bad:
            print(f"  {paint(ui.BAD_MARK, 'red')} {p}")
        die(f"{len(bad)} problem(s): nothing was changed")
    if job["mode"] == "inplace":
        look_inside(job)
    print(f"Rename {old} to {new}: " + ("it leaves the cluster and joins again under the new name (no guests on it)"
                                       if job["mode"] == "rejoin" else "in place (it reboots)"))
    show_plan(job)
    if dry:
        print("dry run - nothing was changed")
        return
    job["log"] = use_log()
    job["step"] = "confirm"
    confirm_yes("", typed=True)
    n = step_count(job)
    try:
        k = next_step(job, "backup", 1, n, "backups")
        back_up(job)
        save(job, "changing")
        finished = (leave_rejoin if job["mode"] == "rejoin" else in_place)(job, k, n)
    except (LabError, KeyboardInterrupt) as e:
        die(failure(job, e))
    if finished:
        done(job)


def resume():
    """Finish the rename in STATE_FILE: the steps after an in-place reboot, or the join after a rejoin's.
    Anything else stopped half-way: say where it is and how to undo it."""
    job = load()
    use_log(job.get("log"))
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
        else:
            die(f"it stopped at step '{job['step']}'")
    except (LabError, KeyboardInterrupt) as e:
        die(failure(job, e))
    done(job)


def done(job):
    """The end: old and new name, the host table, VMs that did not start, the log."""
    from .commands.node import node_list
    clear()
    print(paint(f"\n{job['old']} is now {job['new']}.\n", "green"))
    node_list()
    if job.get("not_started"):
        print(paint(f"\nnot running after the rename: {ui.some(job['not_started'], 8)} - see pvelab list", "red"))
    print(paint(f"\nbackup: {job['backup']}\nlog: {ui.LOG}", "dim"))

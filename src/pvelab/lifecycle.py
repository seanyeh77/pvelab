"""Cloning, first boot, snapshots, removing VMs."""

import re
import time

from .config import AGENT_WAIT, bridge, GUEST_USER, is_plain, READY_TIMEOUT, SNAP_INITIAL, TAG
from . import legacy, nets, registry
from .credentials import ssh_keys
from .guest import agent_out, agent_up, last_line
from .inventory import conf_path, is_managed, read_conf, snapshot_names, tag_list, vm_status
from .owners import SHARED
from .networks import host_dns
from .nodes import node_of, on_node, qm_argv
from .resources import conf_disk_gb

from .ui import log
from .util import die, LabError, parallel, run


def ip_config(net, ip):
    """The cloud-init ipconfig0 value for a VM on net."""
    if is_plain(net):
        return "ip=dhcp"
    return f"ip={ip}/{nets.cidr(net).prefixlen},gw={nets.gateway(net)}"


def net_tags(net):
    """The PVE tags of a VM of net: pvelab's, and net-NAME (a VM's net seen in the GUI, and searched by)."""
    name = "dhcp" if net == "dhcp" else nets.net_name(net)
    return f"{TAG};net-{tag_name(name)}"     # a vnet's alias may hold other characters


def tag_name(name):
    """name as a PVE tag may hold it: lowercase, other characters than a-z 0-9 _ . + - as -."""
    return re.sub(r"[^a-z0-9_.+-]", "-", name.lower())


def has_net_tag(tags):
    """Whether a tag list (tag_list) has a net-NAME tag (which name: the net's own host can tell)."""
    return any(t.startswith("net-") for t in tags)


def new_tags(tags, net, drop=()):
    """tags (PVE's, ; separated) with pvelab's and net-NAME of net; net-OLD of the names in drop (a net's
    name before a rename) taken off; the rest (an admin's own tags, net-* too) kept."""
    want = net_tags(net).split(";")
    gone = {f"net-{tag_name(d)}" for d in drop} - set(want)
    keep = [t for t in tag_list(tags) if t not in gone]
    return ";".join(keep + [t for t in want if t not in keep])


def clone_vm(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk_gb, orig=None, board=None, mac=None,
             user=GUEST_USER, owner=None, shared=False, prefix=None):
    """Clone template tpl to vmid and set it up. orig: when tpl is a copy of a template on another
    node, that template's VMID (its record names it, so labels stay the same everywhere). mac: a
    fixed MAC for its network card (a shared VM's), else PVE picks one. user: its login; owner: whose it
    is, shared: the net's shared VM (both in its record: its name need not tell; owners.py).
    board: the live board that shows the stages (else one line is printed)."""
    ipcfg = ip_config(net, ip)
    where = f"VMID {vmid}, {nets.net_name(net)}{' ' + ip if ip else ''}, template {tpl}"
    if board:
        board.stage(vmid, "clone")
        log(f"{name}: {where}")
    else:
        print(f">> {name} ({where})")
    node = node_of(tpl)                      # the clone is made where the template (or its copy) is

    def qm(*args, **kw):                     # qm on that node, where the clone is too
        return run(*on_node(node, "qm", *args), **kw)
    qm("clone", tpl, vmid, "--name", name)
    legacy.write({vmid: {"name": name, "net": net, "ip": ip or None, "user": user, "password": pw,   # recorded
                         "template": str(orig or tpl), "owner": None if shared else owner,    # now: destroy
                         "role": SHARED if shared else None, "prefix": prefix}})    # can remove it if a step fails
    args = ["set", vmid,
            "--memory", mem, "--cores", cores, "--onboot", 1, "--net0", f"virtio{'=' + mac if mac else ''},bridge={bridge(net)}",
            "--ipconfig0", ipcfg, "--ciuser", user, "--cipassword", pw,
            "--sshkeys", "/dev/stdin", "--ciupgrade", "0"]     # the keys on stdin: no file, on any node
    if not is_plain(net):                    # a plain bridge's DHCP gives its DNS
        args += ["--nameserver", host_dns()]
    qm(*args, data=ssh_keys())
    if disk_gb and disk_gb > conf_disk_gb(read_conf(vmid)):   # grown on first boot by cloud-init
        qm("disk", "resize", vmid, "scsi0", f"{disk_gb}G")
    qm("snapshot", vmid, SNAP_INITIAL, "--description", "pvelab: before first boot")
    rows[vmid] = {"name": name, "user": user, "password": pw, "net": net, "ip": ip or "-"}
    if board:
        board.stage(vmid, "start")
    qm("start", vmid)


def boot(vmid, node, board, first=("start",), stage="starting", wait="booting", said="", see="its console"):
    """`qm <first...>` VM vmid on node (start, or reboot with its options), then wait (up to AGENT_WAIT)
    until its guest agent answers, each step a stage on the board. LabError if either fails; for the
    wait: said + 'no guest agent after N min (still booting? see <see>)'."""
    board.stage(vmid, stage)
    run(*on_node(node, "qm", first[0], vmid, *first[1:]))
    board.stage(vmid, wait)
    t0 = time.time()
    while not agent_up(vmid, node=node):
        if time.time() - t0 > AGENT_WAIT:
            raise LabError(f"{said}no guest agent after {AGENT_WAIT // 60} min (still booting? see {see})")
        time.sleep(3)


def with_snapshots(text, key, value):
    """A VM config's text with key set to value in every snapshot section ([name]); the current config
    (qm set changes it) and PVE's [special:...] sections are left as they are."""
    parts = re.split(r"(?m)^(?=\[)", text)
    out = [parts[0]]
    for p in parts[1:]:
        if not p.startswith("[special:"):
            p = re.sub(rf"(?m)^{re.escape(key)}: .*$", f"{key}: {value}", p)
        out.append(p)
    return "".join(out)


def set_conf(vmid, key, value, node=None):
    """qm set --key value, and the same in the VM's snapshots, so a rollback keeps it (PVE has no command
    for a snapshot's config: its section is changed in the file, in place on /etc/pve)."""
    run(*on_node(node or node_of(vmid), "qm", "set", vmid, f"--{key}", value))
    path = conf_path(vmid)
    text = open(path).read()
    new = with_snapshots(text, key, value)
    if new != text:
        with open(path, "w") as f:
            f.write(new)


def has_snapshot(vmid, snap):
    return snap in snapshot_names(vmid)


def take_snapshot(vmid, snap, desc):
    """(Re)take snapshot <snap> of vmid from its current disk state."""
    if has_snapshot(vmid, snap):
        run(*qm_argv(vmid, "delsnapshot", vmid, snap))
    run(*qm_argv(vmid, "snapshot", vmid, snap, "--description", desc))


CLOUD_INIT_LOG = "/var/log/cloud-init-output.log"


def first_boot_state(vmid, node=None):
    """(cloud-init status inside the VM ('running', 'done', ...) or None, its newest output line or
    None when the guest agent does not answer yet) - one agent call. node: the VM's, if the caller knows it."""
    out = agent_out(vmid, "sh", "-c", f"cloud-init status; echo ==; tail -n 5 {CLOUD_INIT_LOG} 2>/dev/null",
                    wait=10, timeout=30, node=node)
    if out is None:
        return None, None
    status, _, tail = out.partition("\n==\n")
    m = re.search(r"^status: (.+)$", status, re.M)
    return (m.group(1).strip() if m else None), last_line(tail)


def shutdown(vmid, node, timeout):
    """A clean shutdown of VM vmid on node, forced off after timeout seconds."""
    run(*on_node(node, "qm", "shutdown", vmid, "--timeout", timeout, "--forceStop", "1"))


def booting(st):
    """Whether cloud-init is still at work, by its status (first_boot_state()): no answer yet, running
    or not started."""
    return not st or st.startswith(("running", "not started"))


def wait_first_boot(ids, board):
    """Wait until cloud-init has finished in every VM, showing each VM's newest cloud-init line on the
    board; return {vmid: status} of those that did (the others are marked failed on the board)."""
    pending, done, t0 = set(ids), {}, time.time()
    nodes = {i: node_of(i) for i in ids}     # looked up once, not on every poll
    for i in ids:
        board.stage(i, "first boot", live=True)
        board.tail(i, "(no guest agent yet)")
    while pending and time.time() - t0 < READY_TIMEOUT:
        for i, (st, line) in parallel(lambda i: first_boot_state(i, nodes[i]), sorted(pending)).items():
            if line:
                board.tail(i, line)
            if not booting(st):
                done[i] = st
                pending.discard(i)
        if pending:
            time.sleep(4)
    for i in pending:
        board.fail(i, f"first boot not finished after {READY_TIMEOUT // 60} min")
    return done


def remove_vm(vmid):
    """Destroy a pvelab VM and forget it: its record (what clone_vm wrote), and a VM made before the registry
    its credentials.tsv row and blocks.json entry."""
    if not is_managed(read_conf(vmid)):
        die(f"internal: refusing to destroy unmanaged VM {vmid}")
    if vm_status(vmid) != "stopped":
        run(*qm_argv(vmid, "stop", vmid))
    run(*qm_argv(vmid, "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1"))
    registry.forget([vmid])
    legacy.drop([vmid])

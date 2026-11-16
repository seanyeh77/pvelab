"""Cloning, first boot, snapshots, removing VMs."""

import re
import time

from .config import AGENT_WAIT, bridge, GUEST_USER, MARKER, READY_TIMEOUT, SNAP_INITIAL, TAG
from . import nets
from .blocks import blocks_load, blocks_save
from .credentials import cred_rows, cred_save, ssh_keys
from .guest import agent_out, agent_up, last_line
from .inventory import conf_path, desc_value, is_managed, read_conf, snapshot_names, vm_status
from .owners import SHARED
from .networks import host_dns
from .nodes import node_of, on_node, qm_argv
from .resources import conf_disk_gb

from .ui import log
from .util import die, LabError, parallel, run


def ip_config(net, ip):
    """The cloud-init ipconfig0 value for a VM on net."""
    if net == "dhcp":
        return "ip=dhcp"
    return f"ip={ip}/{nets.cidr(net).prefixlen},gw={nets.gateway(net)}"


def net_tags(net):
    """The PVE tags of a VM of net: pvelab's, and net-NAME (a VM's net seen in the GUI, and searched by)."""
    name = "dhcp" if net == "dhcp" else nets.net_name(net)
    return f"{TAG};net-{re.sub(r'[^a-z0-9_.+-]', '-', name.lower())}"     # a vnet's alias may hold others


def new_tags(tags, net):
    """tags (PVE's, ; separated) with pvelab's and net-NAME of net; another net-* dropped, the rest kept."""
    want = net_tags(net).split(";")
    old = [t for t in re.split(r"[;, ]+", tags) if t]
    keep = [t for t in old if not t.startswith("net-") or t in want]
    return ";".join(keep + [t for t in want if t not in keep])


def set_net_tag(vmid):
    """VM vmid's PVE tags with its net's current name (net-NAME), pvelab's tag too: after a net rename, and
    after a rollback (PVE gives back the snapshot's tags)."""
    conf = read_conf(vmid) or {}
    have = conf.get("tags", "")
    if (tags := new_tags(have, desc_value(conf, "net") or "dhcp")) != have:
        run(*on_node(conf.get("_node") or node_of(vmid), "qm", "set", vmid, "--tags", tags))


def clone_vm(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk_gb, orig=None, board=None, mac=None,
             user=GUEST_USER, owner=None, shared=False):
    """Clone template tpl to vmid and set it up. orig: when tpl is a copy of a template on another
    node, that template's VMID (the description names it, so labels stay the same everywhere). mac: a
    fixed MAC for its network card (a shared VM's), else PVE picks one. user: its login; owner: whose it
    is, shared: the net's shared VM (both in the description: its name need not tell; owners.py).
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
    desc = f"{MARKER} template={orig or tpl} net={net}" + (f" role={SHARED}" if shared else f" owner={owner}" if owner else "")
    args = ["set", vmid, "--tags", net_tags(net), "--description", desc,
            "--memory", mem, "--cores", cores, "--onboot", 1, "--net0", f"virtio{'=' + mac if mac else ''},bridge={bridge(net)}",
            "--ipconfig0", ipcfg, "--ciuser", user, "--cipassword", pw,
            "--sshkeys", "/dev/stdin", "--ciupgrade", "0"]     # the keys on stdin: no file, on any node
    if net != "dhcp":
        args += ["--nameserver", host_dns()]
    qm(*args, data=ssh_keys())
    if disk_gb and disk_gb > conf_disk_gb(read_conf(vmid)):   # grown on first boot by cloud-init
        qm("disk", "resize", vmid, "scsi0", f"{disk_gb}G")
    qm("snapshot", vmid, SNAP_INITIAL, "--description", "pvelab: before first boot")
    rows[vmid] = {"name": name, "user": user, "password": pw, "net": net, "ip": ip or "-"}
    cred_save(rows)
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
    """Destroy a pvelab VM and forget it (credentials row, blocks) - what clone_vm recorded."""
    if not is_managed(read_conf(vmid)):
        die(f"internal: refusing to destroy unmanaged VM {vmid}")
    if vm_status(vmid) != "stopped":
        run(*qm_argv(vmid, "stop", vmid))
    run(*qm_argv(vmid, "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1"))
    rows = cred_rows()
    if rows.pop(vmid, None) is not None:
        cred_save(rows)
    blocks = blocks_load()
    if blocks.pop(vmid, None) is not None:
        blocks_save(blocks)

"""Cloning, first boot, snapshots, removing VMs."""

import re
import time

from .config import bridge, GUEST_USER, MARKER, READY_TIMEOUT, SNAP_INITIAL, subnet, TAG
from .blocks import blocks_load, blocks_save
from .credentials import cred_rows, cred_save, keyfile
from .guest import agent_up, guest_json, last_line
from .inventory import is_managed, read_conf, snapshot_names, vm_status
from .networks import host_dns, net_label
from .nodes import is_local, node_of, on_node, qm_argv
from .resources import conf_disk_gb

from .ui import log
from .util import die, parallel, run


def clone_vm(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk_gb, orig=None, board=None):
    """Clone template tpl to vmid and set it up. orig: when tpl is a copy of a template on another
    node, that template's VMID (the description names it, so labels stay the same everywhere).
    board: the live board that shows the stages (else one line is printed)."""
    ipcfg = "ip=dhcp" if net == "dhcp" else f"ip={ip}/24,gw={subnet(net)}.1"
    where = f"VMID {vmid}, {net_label(net)}{' ' + ip if ip else ''}, template {tpl}"
    if board:
        board.stage(vmid, "clone")
        log(f"{name}: {where}")
    else:
        print(f">> {name} ({where})")
    node = node_of(tpl)                      # the clone is made where the template (or its copy) is
    run(*on_node(node, "qm", "clone", tpl, vmid, "--name", name))
    keys, data = keyfile(), None
    if not is_local(node):                   # qm reads --sshkeys from a file on its own node: hand it over
        keys, data = "/dev/stdin", open(keys).read()
    args = ["set", vmid, "--tags", TAG, "--description", f"{MARKER} template={orig or tpl} net={net}",
            "--memory", mem, "--cores", cores, "--onboot", 1, "--net0", f"virtio,bridge={bridge(net)}",
            "--ipconfig0", ipcfg, "--ciuser", GUEST_USER, "--cipassword", pw,
            "--sshkeys", keys, "--ciupgrade", "0"]
    if net != "dhcp":
        args += ["--nameserver", host_dns()]
    run(*qm_argv(vmid, *args), data=data)
    if disk_gb and disk_gb > conf_disk_gb(read_conf(vmid)):   # grown on first boot by cloud-init
        run(*qm_argv(vmid, "disk", "resize", vmid, "scsi0", f"{disk_gb}G"))
    run(*qm_argv(vmid, "snapshot", vmid, SNAP_INITIAL, "--description", "pvelab: before first boot"))
    rows[vmid] = {"name": name, "user": GUEST_USER, "password": pw, "net": net, "ip": ip or "-"}
    cred_save(rows)
    if board:
        board.stage(vmid, "start")
    run(*qm_argv(vmid, "start", vmid))


def has_snapshot(vmid, snap):
    return snap in snapshot_names(vmid)


def take_snapshot(vmid, snap, desc):
    """(Re)take snapshot <snap> of vmid from its current disk state."""
    if has_snapshot(vmid, snap):
        run(*qm_argv(vmid, "delsnapshot", vmid, snap))
    run(*qm_argv(vmid, "snapshot", vmid, snap, "--description", desc))


CLOUD_INIT_LOG = "/var/log/cloud-init-output.log"


def first_boot_state(vmid):
    """(cloud-init status inside the VM ('running', 'done', ...) or None, its newest output line or
    None when the guest agent does not answer yet) - one agent call."""
    if not agent_up(vmid):
        return None, None
    r = guest_json("exec", vmid, "--timeout", 10, "--", "sh", "-c",
                   f"cloud-init status; echo ==; tail -n 5 {CLOUD_INIT_LOG} 2>/dev/null", timeout=30) or {}
    out = r.get("out-data", "")
    status, _, tail = out.partition("\n==\n")
    m = re.search(r"^status: (.+)$", status, re.M)
    return (m.group(1).strip() if m else None), last_line(tail)


def first_boot_status(vmid):
    """cloud-init status inside the VM via the guest agent ('running', 'done', ...), or None."""
    return first_boot_state(vmid)[0]


def wait_first_boot(ids, board):
    """Wait until cloud-init has finished in every VM, showing each VM's newest cloud-init line on the
    board; return {vmid: status} of those that did (the others are marked failed on the board)."""
    pending, done, t0 = set(ids), {}, time.time()
    for i in ids:
        board.stage(i, "first boot", live=True)
        board.tail(i, "(no guest agent yet)")
    while pending and time.time() - t0 < READY_TIMEOUT:
        for i, (st, line) in parallel(first_boot_state, sorted(pending)).items():
            if line:
                board.tail(i, line)
            if st and not st.startswith(("running", "not started")):
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

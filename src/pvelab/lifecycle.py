"""Cloning, first boot, snapshots, removing VMs."""

import re
import time

from .config import bridge, GUEST_USER, MARKER, READY_TIMEOUT, SNAP_INITIAL, subnet, TAG
from .blocks import blocks_load, blocks_save
from .credentials import cred_rows, cred_save, keyfile
from .guest import agent_up, guest_json
from .inventory import is_managed, read_conf, snapshot_names, vm_names, vm_status
from .networks import host_dns, net_label
from .nodes import is_local, node_of, on_node, qm_argv
from .resources import conf_disk_gb
from .ui import Progress
from .util import die, parallel, run


def clone_vm(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk_gb, orig=None):
    """Clone template tpl to vmid and set it up. orig: when tpl is a copy of a template on another
    node, that template's VMID (the description names it, so labels stay the same everywhere)."""
    ipcfg = "ip=dhcp" if net == "dhcp" else f"ip={ip}/24,gw={subnet(net)}.1"
    print(f">> {name} (VMID {vmid}, {net_label(net)}{' ' + ip if ip else ''}) <- template {tpl}")
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
    run(*qm_argv(vmid, "start", vmid))


def has_snapshot(vmid, snap):
    return snap in snapshot_names(vmid)


def take_snapshot(vmid, snap, desc):
    """(Re)take snapshot <snap> of vmid from its current disk state."""
    if has_snapshot(vmid, snap):
        run(*qm_argv(vmid, "delsnapshot", vmid, snap))
    run(*qm_argv(vmid, "snapshot", vmid, snap, "--description", desc))


def first_boot_status(vmid):
    """cloud-init status inside the VM via the guest agent ('running', 'done', ...), or None."""
    if not agent_up(vmid):
        return None
    r = guest_json("exec", vmid, "--timeout", 10, "--", "cloud-init", "status", timeout=30) or {}
    m = re.search(r"^status: (.+)$", r.get("out-data", ""), re.M)
    return m.group(1).strip() if m else None


def wait_first_boot(ids):
    """Wait until cloud-init has finished in every VM; return {vmid: status} of those that did."""
    pending, done, t0 = set(ids), {}, time.time()
    names = vm_names(ids)
    bar = Progress(f"first boot (cloud-init, up to {READY_TIMEOUT // 60} min)", len(ids))
    while pending and time.time() - t0 < READY_TIMEOUT:
        for i, st in parallel(first_boot_status, sorted(pending)).items():
            if st and not st.startswith(("running", "not started")):
                done[i] = st
                pending.discard(i)
        bar.update(len(done), running=[names[p] for p in sorted(pending)])
        if pending:
            time.sleep(5)
    bar.finish()
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

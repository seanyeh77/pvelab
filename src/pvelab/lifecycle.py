"""Cloning, first boot, snapshots, removing VMs."""

import json
import re
import subprocess
import time

from .config import CONF_DIR, CORES, GUEST_USER, MARKER, MEM, READY_TIMEOUT, SNAP, SNAP_INITIAL, TAG
from .credentials import cred_save, gen_password, keyfile
from .inventory import is_managed, read_conf, vm_status
from .networks import host_dns, net_bridge, net_label
from .resources import conf_disk_gb
from .util import die, run


def clone_vm(vmid, name, tpl, net, ip, rows, cores=CORES, mem=MEM, disk_gb=None):
    pw = gen_password()
    ipcfg = "ip=dhcp" if net == "dhcp" else f"ip={ip}/24,gw=10.10.{net}.1"
    print(f">> {name} (VMID {vmid}, {net_label(net)}{' ' + ip if ip else ''}) <- template {tpl}")
    run("qm", "clone", tpl, vmid, "--name", name)
    args = ["qm", "set", vmid, "--tags", TAG, "--description", f"{MARKER} template={tpl} net={net}",
            "--memory", mem, "--cores", cores, "--onboot", 1, "--net0", f"virtio,bridge={net_bridge(net)}",
            "--ipconfig0", ipcfg, "--ciuser", GUEST_USER, "--cipassword", pw,
            "--sshkeys", keyfile(), "--ciupgrade", "0"]
    if net != "dhcp":
        args += ["--nameserver", host_dns()]
    run(*args)
    if disk_gb and disk_gb > conf_disk_gb(read_conf(vmid)):   # grown on first boot by cloud-init
        run("qm", "disk", "resize", vmid, "scsi0", f"{disk_gb}G")
    run("qm", "snapshot", vmid, SNAP_INITIAL, "--description", "pvelab: before first boot")
    rows[vmid] = {"name": name, "user": GUEST_USER, "password": pw, "net": net, "ip": ip or "-"}
    cred_save(rows)
    run("qm", "start", vmid)


def has_snapshot(vmid, snap=SNAP):
    try:
        return re.search(rf"^\[{snap}\]$", open(f"{CONF_DIR}/{vmid}.conf").read(), re.M) is not None
    except FileNotFoundError:
        return False


def take_snapshot(vmid, snap, desc):
    """(Re)take snapshot <snap> of vmid from its current disk state."""
    if has_snapshot(vmid, snap):
        run("qm", "delsnapshot", vmid, snap)
    run("qm", "snapshot", vmid, snap, "--description", desc)


def first_boot_status(vmid):
    """cloud-init status inside the VM via the guest agent ('running', 'done', ...), or None."""
    try:
        if subprocess.run(["qm", "guest", "cmd", str(vmid), "ping"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=15).returncode:
            return None
        r = subprocess.run(["qm", "guest", "exec", str(vmid), "--timeout", "10", "--", "cloud-init", "status"],
                           capture_output=True, text=True, timeout=30)
        m = re.search(r"^status: (.+)$", json.loads(r.stdout).get("out-data", ""), re.M)
        return m.group(1).strip() if m else None
    except (subprocess.TimeoutExpired, ValueError, AttributeError):
        return None


def wait_first_boot(ids):
    """Wait until cloud-init has finished in every VM; return {vmid: status} of those that did."""
    pending, done, t0 = set(ids), {}, time.time()
    print(f">> waiting for {len(pending)} VM(s) to finish first boot (cloud-init, up to {READY_TIMEOUT // 60} min)")
    while pending and time.time() - t0 < READY_TIMEOUT:
        for i in sorted(pending):
            st = first_boot_status(i)
            if st and not st.startswith(("running", "not started")):
                done[i] = st
                pending.discard(i)
        if pending:
            time.sleep(5)
    return done


def remove_vm(vmid):
    if not is_managed(read_conf(vmid)):
        die(f"internal: refusing to destroy unmanaged VM {vmid}")
    if vm_status(vmid) != "stopped":
        run("qm", "stop", vmid)
    run("qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1")


def snapshot_names(vmid):
    try:
        return re.findall(r"^\[([^\]]+)\]$", open(f"{CONF_DIR}/{vmid}.conf").read(), re.M)
    except FileNotFoundError:
        return []

"""pvelab resize"""

from ..args import opt
from ..config import FORCE
from ..helptext import bad_usage
from ..inventory import read_conf, targets, vm_status
from ..resources import conf_disk_gb, host_mem_mb, parse_cores, parse_disk, parse_mem
from ..ui import Progress
from ..util import die, run, warn


def cmd_resize(args):
    cores = mem = disk = None
    reboot, dry, rest = False, False, []
    it = iter(args)
    for a in it:
        if (v := opt(a, it, "--cores")) is not None:
            cores = parse_cores(v)
        elif (v := opt(a, it, "--memory")) is not None:
            mem = parse_mem(v)
        elif (v := opt(a, it, "--disk")) is not None:
            disk = parse_disk(v)
        elif a == "--reboot":
            reboot = True
        elif a == "--dry-run":
            dry = True
        else:
            rest.append(a)
    if cores is None and mem is None and disk is None:
        bad_usage("what to change: --cores, --memory and/or --disk")
    ids = targets(rest)
    confs = {i: read_conf(i) for i in ids}
    if disk and not disk[0]:                 # absolute size: refuse to shrink anything
        small = [f"{confs[i]['name']}({conf_disk_gb(confs[i]):g}G)" for i in ids if disk[1] < conf_disk_gb(confs[i])]
        if small:
            die(f"--disk {disk[1]}: disks can only grow, these are bigger already: {' '.join(small)}")
    plan = {i: changes(i, confs[i], cores, mem, disk) for i in ids}
    if dry:
        print("dry run - would change:")
        for i in ids:
            print(f"  {confs[i]['name']}: " + (", ".join(plan[i][0]) or "nothing to change"))
    if mem:
        more = sum(max(0, mem - int(confs[i].get("memory", 0))) for i in ids)
        total, avail = host_mem_mb()
        if more:
            print(f"memory: {more} MB more for these VMs; the host has {avail} MB available of {total} MB")
        if more and more + 2048 > avail:
            warn(f"memory: {more} MB more needed, {avail} MB available")
            if not FORCE and not dry:
                die("not enough memory on this host (LAB_FORCE=1 to do it anyway)")
    if dry:
        return
    pending = []
    for i in ids:
        what, cmds = plan[i]
        for cmd in cmds:
            run(*cmd)
        print(f"{confs[i]['name']}: " + (", ".join(what) or "nothing to change"))
        if cmds and vm_status(i) == "running":
            pending.append(i)
    if not pending:
        return
    if reboot:
        bar = Progress("reboot", len(pending))
        for n, i in enumerate(pending):
            bar.update(n, running=[confs[i]["name"]])
            run("qm", "reboot", i, "--timeout", "120")
        bar.update(len(pending))
        bar.finish()
    else:
        print(f"\ntakes effect after a reboot (the disk's filesystem grows then too):\n"
              f"  pvelab reboot {' '.join(confs[i]['name'] for i in pending)}   (or add --reboot)")


def changes(vmid, c, cores, mem, disk):
    """(descriptions, qm commands) of what resize would change on VM vmid (config c)."""
    out, sets = [], []
    if cores is not None and int(c.get("cores", 1)) != cores:
        sets += ["--cores", cores]
        out.append(f"cores {c.get('cores', 1)}->{cores}")
    if mem is not None and int(c.get("memory", 0)) != mem:
        sets += ["--memory", mem]
        out.append(f"memory {c.get('memory')}->{mem} MB")
    cmds = [("qm", "set", vmid, *sets)] if sets else []
    if disk:
        cur = conf_disk_gb(c)
        target = cur + disk[1] if disk[0] else disk[1]
        if target > cur:
            cmds.append(("qm", "disk", "resize", vmid, "scsi0", f"{target:g}G"))
            out.append(f"disk {cur:g}->{target:g} GB")
    return out, cmds

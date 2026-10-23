"""pvelab resize"""

from ..args import opt
from ..config import FORCE
from ..helptext import bad_usage
from ..inventory import read_conf, targets, vm_names, vm_statuses
from ..resources import conf_disk_gb, host_mem_mb, mem_shortfall, parse_cores, parse_disk, parse_mem
from ..ui import dry_run, Progress, some
from ..util import die, run, warn


def cmd_resize(args, dry=False):
    cores = mem = gb = None
    rel = False                              # --disk +N: gb is how much to add
    reboot, rest = False, []
    it = iter(args)
    for a in it:
        if (v := opt(a, it, "--cores")) is not None:
            cores = parse_cores(v)
        elif (v := opt(a, it, "--memory")) is not None:
            mem = parse_mem(v)
        elif (v := opt(a, it, "--disk")) is not None:
            rel, gb = parse_disk(v)
        elif a == "--reboot":
            reboot = True
        else:
            rest.append(a)
    if cores is None and mem is None and gb is None:
        bad_usage("what to change: --cores, --memory and/or --disk")
    ids = targets(rest)
    confs = {i: read_conf(i) for i in ids}
    names = vm_names(ids, confs)
    if gb is not None and not rel:                     # absolute size: refuse to shrink anything
        small = [f"{names[i]}({conf_disk_gb(confs[i]):g}G)" for i in ids if gb < conf_disk_gb(confs[i])]
        if small:
            die(f"--disk {gb}: disks can only grow, these are bigger already: {some(small, 10, ' ')}")
    plan = {i: changes(i, confs[i], cores, mem, rel, gb) for i in ids}
    more = sum(max(0, mem - int(confs[i].get("memory", 0))) for i in ids) if mem else 0
    if more:
        total, avail = host_mem_mb()
        print(f"memory: {more} MB more for these VMs; the host has {avail} MB available of {total} MB")
    short = mem_shortfall(more, "these VMs")
    if short:
        warn(short)
    if dry:
        return dry_run("change", [f"{names[i]}: {plan[i][0]}"
                                  for i in ids])
    if short and not FORCE:
        die("not enough memory on this host (LAB_FORCE=1 to do it anyway)")
    pending, status = [], vm_statuses(ids)
    for i in ids:
        what, cmds = plan[i]
        for cmd in cmds:
            run(*cmd)
        print(f"{names[i]}: {what}")
        if cmds and status[i] == "running":
            pending.append(i)
    if not pending:
        return
    if reboot:
        Progress.each("reboot", pending, lambda i: names[i],
                      lambda i: run("qm", "reboot", i, "--timeout", "120"))
    else:
        print(f"\ntakes effect after a reboot (the disk's filesystem grows then too):\n"
              f"  pvelab reboot {' '.join(names[i] for i in pending)}   (or add --reboot)")


def changes(vmid, c, cores, mem, rel, gb):
    """(description, qm commands) of what resize would change on VM vmid (config c)."""
    out, sets = [], []
    if cores is not None and int(c.get("cores", 1)) != cores:
        sets += ["--cores", cores]
        out.append(f"cores {c.get('cores', 1)}->{cores}")
    if mem is not None and int(c.get("memory", 0)) != mem:
        sets += ["--memory", mem]
        out.append(f"memory {c.get('memory')}->{mem} MB")
    cmds = [("qm", "set", vmid, *sets)] if sets else []
    if gb is not None:                       # rel: gb is how much to add
        cur = conf_disk_gb(c)
        target = cur + gb if rel else gb
        if target > cur:
            cmds.append(("qm", "disk", "resize", vmid, "scsi0", f"{target:g}G"))
            out.append(f"disk {cur:g}->{target:g} GB")
    return ", ".join(out) or "nothing to change", cmds

"""pvelab resize"""

from ..args import opt
from ..config import FORCE
from ..helptext import bad_usage
from ..inventory import read_conf, targets, vm_status
from ..resources import conf_disk_gb, mem_available_mb, parse_cores, parse_disk, parse_mem
from ..util import die, run, warn


def cmd_resize(args):
    cores = mem = disk = None
    reboot, rest = False, []
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
    if mem:
        more = sum(max(0, mem - int(confs[i].get("memory", 0))) for i in ids)
        if more and more + 2048 > mem_available_mb():
            warn(f"memory: {more} MB more needed, {mem_available_mb()} MB available")
            if not FORCE:
                die("not enough memory on this host (LAB_FORCE=1 to do it anyway)")
    pending = []
    for i in ids:
        c, name, done = confs[i], confs[i]["name"], []
        sets = []
        if cores is not None and int(c.get("cores", 1)) != cores:
            sets += ["--cores", cores]
            done.append(f"cores {c.get('cores', 1)}->{cores}")
        if mem is not None and int(c.get("memory", 0)) != mem:
            sets += ["--memory", mem]
            done.append(f"memory {c.get('memory')}->{mem} MB")
        if sets:
            run("qm", "set", i, *sets)
        if disk:
            cur = conf_disk_gb(c)
            target = cur + disk[1] if disk[0] else disk[1]
            if target > cur:
                run("qm", "disk", "resize", i, "scsi0", f"{target:g}G")
                done.append(f"disk {cur:g}->{target:g} GB")
        print(f"{name}: " + (", ".join(done) if done else "nothing to change"))
        if done and vm_status(i) == "running":
            pending.append(i)
    if not pending:
        return
    names = " ".join(confs[i]["name"] for i in pending)
    if reboot:
        for i in pending:
            run("qm", "reboot", i, "--timeout", "120")
            print(f"{confs[i]['name']}: rebooted")
    else:
        print(f"\ntakes effect after a reboot (the disk's filesystem grows then too): {names}\n"
              f"  pvelab reboot {' '.join(confs[i]['name'] for i in pending)}   (or add --reboot)")

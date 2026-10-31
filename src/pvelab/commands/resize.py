"""pvelab resize"""

from ..args import opt
from ..config import FORCE
from ..helptext import bad_usage
from ..inventory import read_conf, targets, vm_names, vm_statuses
from ..nodes import here_first, is_local, on_node, qm_argv
from ..resources import conf_disk_gb, host_mem_mb, mem_shortfall, parse_cores, parse_disk, parse_mem
from ..ui import dry_run, each, some
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
    need = {}                                # {node: MB more} for the VMs there
    for i in ids:
        extra = max(0, mem - int(confs[i].get("memory", 0))) if mem else 0
        if extra:
            node = confs[i]["_node"]
            need[node] = need.get(node, 0) + extra
    shorts = []
    for node in here_first(need):
        total, avail = host_mem_mb(node)
        if is_local(node):
            print(f"memory: {need[node]} MB more for these VMs; the host has {avail} MB available of {total} MB")
        else:
            print(f"memory: {need[node]} MB more for VMs on {node}; it has {avail} MB available of {total} MB")
        shorts.append(mem_shortfall(need[node], "these VMs", node, avail))
    shorts = [s for s in shorts if s]
    for short in shorts:
        warn(short)
    if dry:
        return dry_run("change", [f"{names[i]}: {plan[i][0]}"
                                  for i in ids])
    if shorts and not FORCE:
        die(f"not enough memory on {'this' if all(map(is_local, need)) else 'a'} host (LAB_FORCE=1 to do it anyway)")
    pending, status = [], vm_statuses(ids)

    def resize(i, board):
        what, cmds = plan[i]
        board.stage(i, "resize")
        for cmd in cmds:
            run(*on_node(confs[i]["_node"], *cmd))
        if cmds and status[i] == "running" and reboot:
            board.stage(i, "reboot")
            run(*qm_argv(i, "reboot", i, "--timeout", "120"))
            what += ", rebooted"
        elif cmds and status[i] == "running":
            pending.append(i)
            what += " (after a reboot)"
        return what
    each(f"resize: {len(ids)} VM(s)", ids, names, resize, workers=1)
    if pending:
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

"""pvelab resize"""

from ..args import opt
from ..config import FORCE
from ..helptext import bad_usage
from ..inventory import read_conf, targets, vm_names, vm_statuses
from ..nodes import is_local, node_of, on_node, qm_argv
from ..resources import conf_disk_gb, host_mem_mb, mem_shortfall, parse_cores, parse_disk, parse_mem
from ..scripts import board_hosts
from ..ui import Board, dry_run, some
from ..util import die, LabError, run, warn


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
    more, elsewhere = 0, {}                  # MB more: here, and {node: MB} on other nodes
    for i in ids:
        extra = max(0, mem - int(confs[i].get("memory", 0))) if mem else 0
        node = node_of(i)
        if is_local(node):
            more += extra
        elif extra:
            elsewhere[node] = elsewhere.get(node, 0) + extra
    if more:
        total, avail = host_mem_mb()
        print(f"memory: {more} MB more for these VMs; the host has {avail} MB available of {total} MB")
    shorts = [mem_shortfall(more, "these VMs")]
    for node, mb in sorted(elsewhere.items()):
        total, avail = host_mem_mb(node)
        print(f"memory: {mb} MB more for VMs on {node}; it has {avail} MB available of {total} MB")
        shorts.append(mem_shortfall(mb, "these VMs", node))
    shorts = [s for s in shorts if s]
    for short in shorts:
        warn(short)
    if dry:
        return dry_run("change", [f"{names[i]}: {plan[i][0]}"
                                  for i in ids])
    if shorts and not FORCE:
        die(f"not enough memory on {'a' if elsewhere else 'this'} host (LAB_FORCE=1 to do it anyway)")
    pending, status = [], vm_statuses(ids)
    with Board(f"resize: {len(ids)} VM(s)", names, board_hosts(ids)) as board:
        for i in ids:
            what, cmds = plan[i]
            board.stage(i, "resize")
            try:
                for cmd in cmds:
                    run(*on_node(node_of(i), *cmd))
                if cmds and status[i] == "running" and reboot:
                    board.stage(i, "reboot")
                    run(*qm_argv(i, "reboot", i, "--timeout", "120"))
                    what += ", rebooted"
                elif cmds and status[i] == "running":
                    pending.append(i)
                    what += " (after a reboot)"
                board.ok(i, what)
            except LabError as e:
                board.fail(i, str(e))
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

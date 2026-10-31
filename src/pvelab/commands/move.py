"""pvelab move: students' VMs to another PVE host of the cluster, one VM at a time with `qm migrate`.

What PVE allows shapes it (PVE 9.2):
- a VM with snapshots on LVM-thin can't be migrated (QemuMigrate.pm: "non-migratable snapshot exists"), so
  the move deletes them first (the plan names them) and takes a new 'clean' on the target;
- a linked clone arrives as a full copy (LvmThinPlugin.pm), so the target needs each VM's disk data free;
- running VMs are shut down and migrated offline, then started again on the target.
Only VMs of SDN nets (labN) can move: a bridge net (vmbrN, or vmbr1 without --net) is on one host only.
A student's VMs always go together, to the same host."""

import shlex

from ..args import opt
from ..config import SNAP, STORAGE
from ..credentials import cred_rows
from ..guest import last_line, logged_in
from ..helptext import bad_usage
from ..inventory import managed, net_of, snapshot_names, sorted_nets, targets, vm_names, vm_statuses
from ..isolation import by_student, student_of
from ..lifecycle import boot, take_snapshot
from ..networks import nets_ready_on, refresh_net
from ..nodes import is_local, node_of, on_node, online_host, shell
from ..placement import bridge_host
from ..resources import check_copy_space, host_numbers, mem_shortfall
from ..templates import data_gbs, ensure_snippet
from .. import ui
from ..ui import each, paint, Plan, some, Spinner
from ..util import concurrently, confirm_yes, die, LabError, run

MIGRATE_TIMEOUT = 4 * 3600                   # s one `qm migrate` may take (a big disk over a slow link)


def selection(args, to):
    """The VMs args name, with the rest of each student's VMs: (VMIDs to move, by student then VMID; names
    added so a student stays together; names already on host to; managed() configs; {vmid: name} of the
    VMs to move)."""
    man = managed()
    picked = targets(args)
    name = vm_names(man, man)
    groups = by_student(name)
    full = [i for s in sorted({student_of(name[i]) for i in picked}) for i in groups[s]]
    added = [name[i] for i in full if i not in picked]
    there = [name[i] for i in full if man[i]["_node"] == to]
    moving = [i for i in full if man[i]["_node"] != to]
    return moving, added, there, man, {i: name[i] for i in moving}


def refuse_pinned(ids, man, rows, to):
    """Die if any VM of ids is on a bridge net (vmbrN, or the DHCP bridge): it has a network on one host
    only. Else {vmid: its net}."""
    nets = {i: net_of(i, rows, man[i]) for i in ids}
    pinned = [i for i in ids if bridge_host(nets[i])]
    if not pinned:
        return nets
    can = sorted_nets({n for i, n in nets.items() if i not in pinned})
    die("these VMs can't move: their net is a bridge on one host only (only SDN nets, labN, span hosts):\n  "
        + some([f"{man[i]['name']} ({'no --net' if nets[i] == 'dhcp' else 'net' + nets[i]})" for i in pinned], 8, " ")
        + "".join(f"\n  the others: pvelab move --net {n} --to {to}" for n in can))


def build_plan(ids, man, name, to, status, running, snaps, gb, who, numbers):
    """The plan's steps and warnings (a Plan, not asked yet). name: {vmid: name} of ids; status: {vmid:
    running / ...}; running: the running ones; snaps: {vmid: [snapshot names]}; gb: {vmid: GB of disk data};
    who: logged_in() of the running ones; numbers: to's (MB of memory available, storage()), host_numbers()."""
    plan = Plan()
    users, silent = who
    avail, sto = numbers
    plan.do(f"move {len(ids)} VM(s) of {len(by_student(name))} student(s) to {to}, one VM at a time (qm migrate):"
            + "".join(f"\n       {name[i]:<24} from {man[i]['_node']:<12} {status[i]}" for i in ids))
    if running:
        plan.do(f"shut down the {len(running)} running VM(s) first and start them again on {to}: "
                + some([name[i] for i in running], 6, " "))
    lost = [i for i in ids if snaps[i]]
    if lost:
        plan.do("delete their snapshots - there is no rollback to these afterwards:"
                + "".join(f"\n       {name[i]:<24} {', '.join(snaps[i])}" for i in lost))
    total = sum(gb.values())
    plan.do(f"copy their disks: ~{total:.1f} GB more on {STORAGE} on {to} (full copies, no longer linked clones "
            "of the template)")
    plan.do(f"take a new '{SNAP}' snapshot of each VM on {to}, then update the nets' isolation tables")
    for vm, who_ in sorted(users.items()):
        plan.warn(f"{vm}: {', '.join(who_)} logged in now - they are disconnected while it moves")
    if silent:
        plan.warn(f"no guest agent answer (can't tell who is logged in): {some(silent, 6, ' ')}")
    for p in check_copy_space(total, to, sto):     # dies if it can't fit at all
        plan.warn(p)
    short = mem_shortfall(sum(int(man[i].get("memory", 0)) for i in running), "the running VMs", to, avail)
    if short:
        plan.warn(short)
    return plan


class Stopped(LabError):
    """A move failed before the VM reached the target: the student's other VMs stay where they are."""


def move_vm(i, board, to, running, snaps, src):
    """Move VM i to host to, step by step on its board line; the text its line ends with. A failure says
    which step and what to do: a Stopped before the VM is on to, else a LabError (it is there). src: its host."""
    name, step = board.rows[i]["name"], "shut down"
    try:
        if running:
            board.stage(i, "shut down")
            run(*on_node(src, "qm", "shutdown", i, "--timeout", "120", "--forceStop", "1"))
        step = "delete snapshots"
        for s in reversed(snaps):           # newest first
            board.stage(i, f"delete snapshot {s}")
            run(*on_node(src, "qm", "delsnapshot", i, s))
        step = "migrate"
        board.stage(i, f"migrate to {to}", live=True)
        rc, out = shell(src, f"qm migrate {i} {shlex.quote(to)}", timeout=MIGRATE_TIMEOUT,
                        show=lambda line: board.tail(i, line))
        if rc:
            raise LabError(last_line(out) or f"exit {rc}")
    except LabError as e:
        now = node_of(i) or src
        if now == to:                        # PVE finished after all (e.g. only its exit code was odd)
            ui.log(f"{name}: {step}: {e} - but it is on {to}")
        else:
            advice = {"shut down": "nothing changed",
                      "delete snapshots": "some snapshots may be gone",
                      "migrate": "its snapshots are gone"}[step]
            again = f"; start it: pvelab start {name}" if running and step != "shut down" else ""
            raise Stopped(f"{step}: {e} - still on {now}, {advice}{again}") from e
    try:
        if running:
            step = "start"
            boot(i, to, board, stage=f"start on {to}", wait="boot (guest agent)")
        step = f"snapshot {SNAP}"
        board.stage(i, step)
        take_snapshot(i, SNAP, f"pvelab: after the move to {to}")
    except LabError as e:
        fix = f"pvelab start {name}" if step == "start" else f"pvelab snapshot {name}"
        raise LabError(f"moved to {to}, but {step} failed: {e} - then: {fix}") from e
    return f"on {to}" + (", running" if running else "")


def cmd_move(args, dry=False):
    to, rest = None, []
    it = iter(args)
    for a in it:
        if (v := opt(a, it, "--to")) is not None:
            to = v
        else:
            rest.append(a)
    if not to:
        bad_usage("where to: --to HOST")
    online_host("--to", to)
    ids, added, there, man, name = selection(rest, to)       # targets(): "which VM(s)?" if rest is empty
    if there:
        print(f"already on {to}, skipped: {some(there, 8, ' ')}")
    if not ids:
        print("nothing to move")
        return
    net_of_vm = refuse_pinned(ids, man, cred_rows(), to)
    if added:
        print(f"added, so each student's VMs stay together: {some(added, 8, ' ')}")
    status = vm_statuses(ids)
    running = [i for i in ids if status[i] == "running"]
    with Spinner("disk data, snapshots and who is logged in"):
        snaps = {i: snapshot_names(i) for i in ids}
        gb, who, numbers = concurrently(lambda: data_gbs({i: man[i] for i in ids}),    # one lvs per host
                                        lambda: logged_in(running, name),
                                        lambda: host_numbers([to])[to])
    plan = build_plan(ids, man, name, to, status, running, snaps, gb, who, numbers)
    plan.show()                              # move asks once below, with a typed yes
    if dry:
        print("dry run - nothing changed")
        return
    confirm_yes(f"Move {len(ids)} VM(s) to {to}? Their snapshots are deleted.")

    nets = sorted_nets(set(net_of_vm.values()))
    with Spinner(f"{to}: vendor-data snippet and the nets' isolation tables"):
        if not is_local(to):
            ensure_snippet(to)               # the moved VMs' cloud-init points at its snippet
            nets_ready_on(to, nets, "no VM was moved")
        for n in nets:
            refresh_net(n)                   # the tables on every host, before a VM starts on to

    stopped = {}                             # {student: the VM whose move failed before it reached to}

    def one(i, board):
        s = student_of(name[i])
        if s in stopped:
            raise LabError(f"not moved: {stopped[s]} of the same student failed")
        try:
            return move_vm(i, board, to, i in running, snaps[i], man[i]["_node"])
        except Stopped:
            stopped[s] = name[i]
            raise
    board = each(f"move to {to}: {len(ids)} VM(s)", ids, name, one, workers=1)

    with Spinner("isolation tables of the nets"):
        for n in nets:
            refresh_net(n)                   # set_isolation: each host's table of the net
    summary(board, ids, name, to, there, stopped, nets)


def summary(board, ids, name, to, there, stopped, nets):
    """The lines after the board: moved, skipped, failed, students left on two hosts, what next."""
    ok = [i for i in ids if board.rows[i]["state"] == "ok"]
    bad = [name[i] for i in ids if i not in ok]
    print()
    print(paint(f"moved {len(ok)}/{len(ids)} VM(s) to {to}", "green" if not bad else "yellow")
          + (f"; skipped (already there): {len(there)}" if there else ""))
    if bad:
        print(paint(f"not moved: {some(bad, 8, ' ')}  (their lines above say why)", "red"))
    groups = by_student(name)
    split = [s for s in stopped if len({node_of(i) for i in groups[s]}) > 1]
    if split:
        print(paint(f"on two hosts now: {', '.join(split)} - once the problem is fixed, move the rest: "
                    + " ".join(f"pvelab move '{s}*' --to {to}" for s in split), "red"))
    print(paint("\nNext:", "bold"))
    for n in nets:
        print(f"  pvelab list --net {n}           where each VM is now")
    if ui.LOG:
        print(paint(f"log: {ui.LOG}", "dim"))

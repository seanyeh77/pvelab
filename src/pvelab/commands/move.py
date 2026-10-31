"""pvelab move: students' VMs to another PVE host of the cluster, one VM at a time with `qm migrate`.

What PVE allows shapes it (PVE 9.2):
- a VM with snapshots on LVM-thin can't be migrated (QemuMigrate.pm: "non-migratable snapshot exists"), so
  the move deletes them first (the plan names them) and takes a new 'clean' on the target;
- a linked clone arrives as a full copy (LvmThinPlugin.pm), so the target needs each VM's disk data free;
- running VMs are shut down and migrated offline, then started again on the target.
Only VMs of SDN nets (labN) can move: a bridge net (vmbrN, or vmbr1 without --net) is on one host only.
A student's VMs always go together, to the same host."""

import shlex
import time

from ..args import opt
from ..config import AGENT_WAIT, SNAP, STORAGE
from ..credentials import cred_rows
from ..guest import agent_up, last_line, logged_in
from ..helptext import bad_usage
from ..inventory import did_you_mean, managed, net_of, snapshot_names, sorted_nets, targets, vm_statuses
from ..isolation import student_of
from ..lifecycle import take_snapshot
from ..networks import ensure_gw_mac, refresh_net
from ..nodes import is_local, members, node_of, on_node, shell
from ..placement import bridge_host
from ..resources import check_copy_space, host_mem_mb, mem_shortfall
from ..sdn import wait_vnet
from ..templates import data_gb, ensure_snippet
from .. import ui
from ..ui import each, paint, Plan, some, Spinner
from ..util import confirm_yes, die, LabError, parallel, run

MIGRATE_TIMEOUT = 4 * 3600                   # s one `qm migrate` may take (a big disk over a slow link)


def valid_target(to):
    """--to's value: an online host of the cluster, or die."""
    m = members()
    if to not in m:
        die(f"--to {to}: no such host in the cluster (hosts: {', '.join(sorted(m))})" + did_you_mean(to, list(m)))
    if not m[to]["online"]:
        die(f"--to {to}: that host is offline")
    return to


def selection(args, to):
    """The VMs args name, with the rest of each student's VMs: (VMIDs to move, by student then VMID; names
    added so a student stays together; names already on host to; managed() configs)."""
    man = managed()
    picked = targets(args)
    name = {i: c.get("name") or str(i) for i, c in man.items()}
    students = {student_of(name[i]) for i in picked}
    full = sorted((i for i in man if student_of(name[i]) in students), key=lambda i: (student_of(name[i]), i))
    added = [name[i] for i in full if i not in picked]
    there = [name[i] for i in full if man[i]["_node"] == to]
    return [i for i in full if man[i]["_node"] != to], added, there, man


def refuse_pinned(ids, man, rows, to):
    """Die if any VM of ids is on a bridge net (vmbrN, or the DHCP bridge): it has a network on one host only."""
    nets = {i: net_of(i, rows, man[i]) for i in ids}
    pinned = [i for i in ids if bridge_host(nets[i])]
    if not pinned:
        return
    can = sorted_nets({n for i, n in nets.items() if i not in pinned})
    die("these VMs can't move: their net is a bridge on one host only (only SDN nets, labN, span hosts):\n  "
        + some([f"{man[i]['name']} ({'no --net' if nets[i] == 'dhcp' else 'net' + nets[i]})" for i in pinned], 8, " ")
        + "".join(f"\n  the others: pvelab move --net {n} --to {to}" for n in can))


def build_plan(ids, man, to, status, snaps, gb, who):
    """The plan's steps and warnings (a Plan, not asked yet). status: {vmid: running / ...}; snaps:
    {vmid: [snapshot names]}; gb: {vmid: GB of disk data}; who: logged_in() of the running ones."""
    plan, name = Plan(), {i: man[i]["name"] for i in ids}
    running = [i for i in ids if status[i] == "running"]
    users, silent = who
    students = len({student_of(n) for n in name.values()})
    plan.do(f"move {len(ids)} VM(s) of {students} student(s) to {to}, one VM at a time (qm migrate):"
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
    for p in check_copy_space(total, to):    # dies if it can't fit at all
        plan.warn(p, force=True)
    short = mem_shortfall(sum(int(man[i].get("memory", 0)) for i in running), "the running VMs", to,
                          host_mem_mb(to)[1])
    if short:
        plan.warn(short, force=True)
    return plan


class Stopped(LabError):
    """A move failed before the VM reached the target: the student's other VMs stay where they are."""


def move_vm(i, board, to, running, snaps):
    """Move VM i to host to, step by step on its board line; the text its line ends with. A failure says
    which step and what to do: a Stopped before the VM is on to, else a LabError (it is there)."""
    src, name, step = node_of(i), board.rows[i]["name"], "shut down"
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
            board.stage(i, f"start on {to}")
            run(*on_node(to, "qm", "start", i))
            board.stage(i, "boot (guest agent)")
            t0 = time.time()
            while not agent_up(i, node=to):
                if time.time() - t0 > AGENT_WAIT:
                    raise LabError(f"no guest agent after {AGENT_WAIT // 60} min (still booting? see its console)")
                time.sleep(3)
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
    if not rest:
        bad_usage("which VM(s)?")
    valid_target(to)
    ids, added, there, man = selection(rest, to)
    if there:
        print(f"already on {to}, skipped: {some(there, 8, ' ')}")
    if not ids:
        print("nothing to move")
        return
    rows = cred_rows()
    refuse_pinned(ids, man, rows, to)
    if added:
        print(f"added, so each student's VMs stay together: {some(added, 8, ' ')}")
    name = {i: man[i]["name"] for i in ids}
    status = vm_statuses(ids)
    running = [i for i in ids if status[i] == "running"]
    with Spinner("disk data, snapshots and who is logged in"):
        snaps = {i: snapshot_names(i) for i in ids}
        gb = parallel(lambda i: data_gb(i, man[i]), ids)        # lvs on each VM's host
        who = logged_in(running, name)
    plan = build_plan(ids, man, to, status, snaps, gb, who)
    plan.show()                              # move asks once below, with a typed yes
    if dry:
        print("dry run - nothing changed")
        return
    confirm_yes(f"Move {len(ids)} VM(s) to {to}? Their snapshots are deleted.")

    nets = sorted_nets({net_of(i, rows, man[i]) for i in ids})
    with Spinner(f"{to}: vendor-data snippet and the nets' isolation tables"):
        if not is_local(to):
            ensure_snippet(to)               # the moved VMs' cloud-init points at it
            for n in nets:
                ensure_gw_mac(n)             # VMs on another host reach the gateway by its fixed MAC
                if not wait_vnet(to, n):
                    die(f"net{n}'s vnet is not up on {to} after 1 min; no VM was moved. On {to}: ifreload -a")
        for n in nets:
            refresh_net(n)                   # the tables on every host, before a VM starts on to

    stopped = {}                             # {student: the VM whose move failed before it reached to}

    def one(i, board):
        s = student_of(name[i])
        if s in stopped:
            raise LabError(f"not moved: {stopped[s]} of the same student failed")
        try:
            return move_vm(i, board, to, i in running, snaps[i])
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
    split = [s for s in stopped if len({node_of(i) for i in ids if student_of(name[i]) == s}) > 1]
    if split:
        print(paint(f"on two hosts now: {', '.join(split)} - once the problem is fixed, move the rest: "
                    + " ".join(f"pvelab move '{s}*' --to {to}" for s in split), "red"))
    print(paint("\nNext:", "bold"))
    for n in nets:
        print(f"  pvelab list --net {n}           where each VM is now")
    if ui.LOG:
        print(paint(f"log: {ui.LOG}", "dim"))

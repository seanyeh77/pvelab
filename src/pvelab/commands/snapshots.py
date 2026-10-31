"""pvelab rollback / snapshot"""

from ..args import opt
from ..config import SNAP, SNAP_INITIAL
from ..inventory import snapshot_names, targets, vm_names
from ..nodes import qm_argv
from ..lifecycle import has_snapshot, take_snapshot
from ..scripts import board_hosts
from ..ui import Board, dry_run, paint, some
from ..util import confirm_yes, die, LabError, parallel, run


def each(title, ids, names, stage, work, done):
    """work(vmid) on every VM, a few at a time, each on its own line of a live board."""
    with Board(title, names, board_hosts(ids)) as board:
        def one(i):
            board.stage(i, stage)
            try:
                work(i)
                board.ok(i, done)
            except LabError as e:
                board.fail(i, str(e))
        parallel(one, ids, workers=4)
    failed = board.counts()[1]
    if failed:
        print(paint(f"{failed} VM(s) failed: see their lines above", "red"))
    return board


def cmd_rollback(args, dry=False):
    snap, rest = SNAP, []
    it = iter(args)
    for a in it:
        if a == "--initial":
            snap = SNAP_INITIAL
        elif (v := opt(a, it, "--to")) is not None:
            snap = v
        else:
            rest.append(a)
    ids = targets(rest)
    names = vm_names(ids)
    missing = [names[i] for i in ids if not has_snapshot(i, snap)]
    if missing:
        have = sorted({n for i in ids for n in snapshot_names(i)})
        die(f"no '{snap}' snapshot on: {some(missing, 10, ' ')}"
            + (f" (snapshots there: {', '.join(have)})" if have else " (no snapshots there; take one with 'pvelab snapshot')"))
    if dry:
        return dry_run(f"roll back to '{snap}' (everything done since is lost) and start",
                       [f"{names[i]} ({i})" for i in ids])
    confirm_yes(f"This will roll back {len(ids)} VM(s) to '{snap}' and start them - everything done since is lost:\n  "
                + some([f"{names[i]}({i})" for i in ids], 12, " "))
    each(f"rollback to '{snap}': {len(ids)} VM(s)", ids, names, f"rollback {snap}",
         lambda i: run(*qm_argv(i, "rollback", i, snap, "--start", "1")), f"back at '{snap}', started")


def cmd_snapshot(args):
    ids = targets(args)
    each(f"snapshot '{SNAP}': {len(ids)} VM(s)", ids, vm_names(ids), f"snapshot {SNAP}",
         lambda i: take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'"), f"'{SNAP}' = current state")
    print(paint(f"back to it later: pvelab rollback <VM>   (rolls back to '{SNAP}')", "dim"))

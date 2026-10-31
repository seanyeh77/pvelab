"""pvelab rollback / snapshot"""

from ..args import opt
from ..config import SNAP, SNAP_INITIAL
from ..inventory import snapshot_names, targets, vm_names
from ..nodes import qm_argv
from ..lifecycle import has_snapshot, take_snapshot
from ..ui import dry_run, each, paint, some
from ..util import confirm_yes, die, run


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

    def roll(i, board):
        board.stage(i, f"rollback {snap}")
        run(*qm_argv(i, "rollback", i, snap, "--start", "1"))
        return f"back at '{snap}', started"
    each(f"rollback to '{snap}': {len(ids)} VM(s)", ids, names, roll)


def cmd_snapshot(args):
    ids = targets(args)

    def snap(i, board):
        board.stage(i, f"snapshot {SNAP}")
        take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'")
        return f"'{SNAP}' = current state"
    each(f"snapshot '{SNAP}': {len(ids)} VM(s)", ids, vm_names(ids), snap)
    print(paint(f"back to it later: pvelab rollback <VM>   (rolls back to '{SNAP}')", "dim"))

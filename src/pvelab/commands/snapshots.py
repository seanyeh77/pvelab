"""pvelab rollback / snapshot"""

from ..args import opt
from ..config import SNAP, SNAP_INITIAL
from ..inventory import snapshot_names, targets, vm_names
from ..nodes import qm_argv
from ..lifecycle import has_snapshot, take_snapshot
from ..ui import dry_run, some
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
    confirm_yes(f"Roll back to '{snap}' (everything done since is lost): "
                + " ".join(f"{names[i]}({i})" for i in ids))
    for i in ids:
        run(*qm_argv(i, "rollback", i, snap, "--start", "1"))
        print(f"{names[i]}: rolled back to '{snap}' and started")


def cmd_snapshot(args):
    ids = targets(args)
    for i, name in vm_names(ids).items():
        take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'")
        print(f"{name}: '{SNAP}' snapshot = current state")

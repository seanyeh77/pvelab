"""pvelab rollback / snapshot"""

from ..args import opt
from ..config import SNAP, SNAP_INITIAL
from ..inventory import read_conf, targets
from ..lifecycle import has_snapshot, snapshot_names, take_snapshot
from ..util import confirm_yes, die, run


def cmd_rollback(args):
    snap, rest, dry = SNAP, [], False
    it = iter(args)
    for a in it:
        if a == "--initial":
            snap = SNAP_INITIAL
        elif (v := opt(a, it, "--to")) is not None:
            snap = v
        elif a == "--dry-run":
            dry = True
        else:
            rest.append(a)
    ids = targets(rest)
    names = {i: read_conf(i)["name"] for i in ids}
    missing = [names[i] for i in ids if not has_snapshot(i, snap)]
    if missing:
        have = sorted({n for i in ids for n in snapshot_names(i)} - {"pending"})
        die(f"no '{snap}' snapshot on: {' '.join(missing)}"
            + (f" (snapshots there: {', '.join(have)})" if have else " (no snapshots there; take one with 'pvelab snapshot')"))
    if dry:
        print(f"dry run - would roll back to '{snap}' (everything done since is lost) and start:")
        for i in ids:
            print(f"  {names[i]} ({i})")
        return
    confirm_yes(f"Roll back to '{snap}' (everything done since is lost): "
                + " ".join(f"{names[i]}({i})" for i in ids))
    for i in ids:
        run("qm", "rollback", i, snap, "--start", "1")
        print(f"{names[i]}: rolled back to '{snap}' and started")


def cmd_snapshot(args):
    ids = targets(args)
    for i in ids:
        name = read_conf(i)["name"]
        take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'")
        print(f"{name}: '{SNAP}' snapshot = current state")

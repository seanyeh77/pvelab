"""pvelab rollback / snapshot"""

import time

from ..args import opt
from ..config import READY_TIMEOUT, SNAP, SNAP_INITIAL
from ..inventory import read_conf, snapshot_names, targets, vm_names
from ..nodes import qm_argv
from ..lifecycle import booting, first_boot_state, has_snapshot, set_net_tag, take_snapshot
from ..ui import dry_run, each, paint, some
from ..util import confirm_yes, die, LabError, run


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
        before = (read_conf(i) or {}).get("tags")
        run(*qm_argv(i, "rollback", i, snap, "--start", "1"))
        try:                                 # the tags it had: PVE gave back the snapshot's
            set_net_tag(i, base=before)
        except LabError as e:
            return f"back at '{snap}', started; its net tag not set again ({e})"
        if snap == SNAP_INITIAL:             # before its first boot: ready once cloud-init has set it up again
            first_boot(i, board)
            return f"back at '{snap}', first boot done"
        return f"back at '{snap}', started"
    each(f"rollback to '{snap}': {len(ids)} VM(s)", ids, names, roll)


def first_boot(i, board):
    """Wait (up to READY_TIMEOUT) until VM i's first boot is done, its newest cloud-init line on the board:
    a command right after (a rename's reboot, a passwd) finds it set up, not halfway."""
    board.stage(i, "first boot", live=True)
    t0 = time.time()
    while booting((state := first_boot_state(i))[0]):
        if state[1]:
            board.tail(i, state[1])
        if time.time() - t0 > READY_TIMEOUT:
            raise LabError(f"first boot not finished after {READY_TIMEOUT // 60} min (see its console)")
        time.sleep(4)


def cmd_snapshot(args):
    ids = targets(args)

    def snap(i, board):
        board.stage(i, f"snapshot {SNAP}")
        take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'")
        return f"'{SNAP}' = current state"
    each(f"snapshot '{SNAP}': {len(ids)} VM(s)", ids, vm_names(ids), snap)
    print(paint(f"back to it later: pvelab rollback <VM>   (rolls back to '{SNAP}')", "dim"))
